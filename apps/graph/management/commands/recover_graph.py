from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """恢复卡死的图谱构建状态（手动运维入口）

    典型场景：图谱抽取任务被硬超时（SIGKILL）/worker 崩溃杀死后，
    文档卡在 graph_status='extracting'，pending 文档失去触发源。
    本命令扫描并回退卡死文档、重新派发节点任务，逻辑与自愈定时任务
    graph_recover_task 完全一致（共用 _recover_stuck_graph_docs）。

    默认（不带 --force）与自愈任务行为相同：跳过有活跃任务标记的节点，
    避免误回退正在抽取的文档。--force 用于人工确认 worker 空闲的场景
    （如批量上传后异常重启），跳过活跃标记检查直接恢复。
    """

    help = '恢复卡死的图谱构建状态（回退 extracting 并重新派发任务）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='跳过活跃任务标记检查，强制回退所有 extracting 文档（需人工确认 worker 空闲）',
        )

    def handle(self, *args, **options):
        from apps.graph.tasks import _recover_stuck_graph_docs

        stats = _recover_stuck_graph_docs(force=options['force'])
        self.stdout.write(self.style.SUCCESS(
            f'恢复完成: 回退并重新派发 {stats["recovered"]} 个文档, '
            f'补派 {stats["dispatched_nodes"]} 个节点任务'))
        if stats['recovered'] == 0 and stats['dispatched_nodes'] == 0:
            self.stdout.write('没有发现卡死的图谱构建状态')
