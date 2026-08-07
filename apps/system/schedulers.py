"""基于 SystemConfig 的动态 Beat 调度器

默认 celery beat（PersistentScheduler）只在进程启动时加载 app.conf.beat_schedule，
此后调度时间固定，改调度必须改代码并重启 beat。

本调度器在 PersistentScheduler 基础上增加"运行期重载"：
- 每个 tick（受 CELERY_BEAT_MAX_LOOP_INTERVAL 约束，默认 30s）从 SystemConfig
  读取最新调度快照，与上一次快照比对，有变化才重建 self.schedule。
- 复用旧 ScheduleEntry 的 last_run_at / total_run_count，避免重建后任务被
  "当作从未运行"而立即触发；已停用任务从调度中移除，重新启用时按新时间正常排队。
- 持久化行为与原生 beat 一致（shelve 文件），beat 重启后 last_run_at 不丢失。
- DB 短暂不可用时保留当前调度并回退注册表默认值，beat 不因此崩溃。

使用方式：settings 中配置 CELERY_BEAT_SCHEDULER 指向本类，`celery beat` 命令
即自动使用本调度器，无需改动启动脚本。
"""
import copy
import json

from celery.beat import PersistentScheduler
from loguru import logger


class SystemConfigScheduler(PersistentScheduler):
    """从 SystemConfig 读取调度配置并运行期热更新的 Beat Scheduler"""

    # 上一次加载的调度快照：{name: {'task','cron','enabled'}}，用于判断是否需要重建
    _last_snapshot = None

    def setup_schedule(self):
        """启动时先按原生流程加载持久化文件与默认调度，再用 SystemConfig 覆盖

        覆盖逻辑复用运行期重载入口，保证启动与热更新的重建路径完全一致。
        """
        super().setup_schedule()
        self._reload_from_config(initial=True)

    def tick(self, *args, **kwargs):
        """每个 tick 前检查调度配置是否有变化，有则热更新后再执行原生 tick"""
        self._reload_from_config()
        return super().tick(*args, **kwargs)

    def _reload_from_config(self, initial=False):
        """读取 SystemConfig 调度快照并重建 self.schedule（内容有变化才重建）

        - 读取/解析失败：保留当前调度（initial 时以默认调度兜底），beat 不崩溃
        - 成功但无变化：跳过重建，避免无谓的 IO 与日志噪声
        """
        from apps.system.scheduler_registry import load_schedule_snapshot

        try:
            snapshot = load_schedule_snapshot()
        except Exception as e:
            level = 'warning' if initial else 'debug'
            getattr(logger, level)(f'[beat] 读取调度配置失败，保留当前调度: {e}')
            return

        if snapshot == self._last_snapshot:
            return

        try:
            self._apply_snapshot(snapshot)
        except Exception as e:
            logger.exception(f'[beat] 重建调度失败，保留当前调度: {e}')

    def _apply_snapshot(self, snapshot):
        """用快照重建 self.schedule（新增/更新/移除任务条目）

        更新已存在的条目时复用原 ScheduleEntry（仅替换 task/schedule/options），
        保留 last_run_at 与 total_run_count，确保调度重载不会造成任务立即重复执行。
        """
        from apps.system.scheduler_registry import build_crontab

        schedule = self.schedule
        # 移除已从注册表下架的任务，保留 celery 默认清理条目（若启用）
        for name in list(schedule):
            if name not in snapshot and name != 'celery.backend_cleanup':
                schedule.pop(name)

        for name, spec in snapshot.items():
            entry = schedule.get(name)
            # 归一化 spec 中的 options（注册表不提供时为空 dict），避免键缺失
            options = spec.get('options') or {}
            if entry is not None:
                entry.update(self.Entry(
                    name=name, task=spec['task'],
                    schedule=build_crontab(spec['cron']),
                    options=options, app=self.app,
                ))
            else:
                schedule[name] = self.Entry(
                    name=name, task=spec['task'],
                    schedule=build_crontab(spec['cron']),
                    options=options, app=self.app,
                )

        # 兜底注入默认条目（如 celery.backend_cleanup），保证与原生 beat 行为一致
        self.install_default_entries(schedule)

        self._last_snapshot = copy.deepcopy(snapshot)
        try:
            self.sync()
        except Exception as e:
            # 持久化失败不阻断调度生效，仅记录告警（内存中的调度已更新）
            logger.warning(f'[beat] 调度持久化失败: {e}')
        changed = json.dumps(
            {name: {'cron': s['cron'], 'enabled': s.get('enabled', True)}
             for name, s in snapshot.items()},
            ensure_ascii=False,
        )
        logger.info(f'[beat] 调度配置已热更新，当前 {len(snapshot)} 个任务: {changed}')
