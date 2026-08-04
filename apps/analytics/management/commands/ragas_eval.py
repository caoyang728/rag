"""
Ragas 全自动评估命令

用法示例:
  # 全自动:从知识库文档生成测试集并评估(默认 20 题)
  docker compose exec django python manage.py ragas_eval

  # 指定测试集大小与语料范围
  docker compose exec django python manage.py ragas_eval --testset-size 30 --limit-docs 80 --root-type company_doc

  # 指定评估模型(默认用项目 LLM_BASE_MODEL)
  docker compose exec django python manage.py ragas_eval --model deepseek-chat

  # 复用已生成的测试集,跳过生成步骤(--testset 指向 JSON 文件)
  docker compose exec django python manage.py ragas_eval --skip-generate \
      --testset eval_reports/testset_20260802_120000_abc123.json

前置条件:
  1. pip install -r requirements-eval.txt
  2. 知识库中已有 status=done 的文档
  3. LLM_API_KEY / Embedding 服务可用

输出:
  eval_reports/testset_<id>.json   测试集(可复用、可人工抽检)
  eval_reports/report_<id>.json    评估明细(JSON)
  eval_reports/report_<id>.md      评估摘要(Markdown)
"""
import os

from django.core.management.base import BaseCommand
from loguru import logger


class Command(BaseCommand):
    help = 'Ragas 全自动评估:从知识库文档生成测试集 → 跑 RAG → Ragas 标准指标评估 → 报告'

    def add_arguments(self, parser):
        parser.add_argument(
            '--testset-size', type=int, default=20,
            help='生成的测试样本数(默认 20)',
        )
        parser.add_argument(
            '--limit-docs', type=int, default=50,
            help='取多少篇文档作为语料(默认 50,控制生成成本)',
        )
        parser.add_argument(
            '--root-type', type=str, default='',
            help='限定领域(默认全部)',
        )
        parser.add_argument(
            '--model', type=str, default='',
            help='评估/生成用 LLM 模型(默认项目 LLM_BASE_MODEL)',
        )
        parser.add_argument(
            '--output-dir', type=str, default='eval_reports',
            help='报告输出目录(默认 eval_reports)',
        )
        parser.add_argument(
            '--skip-generate', action='store_true',
            help='跳过测试集生成,复用 --testset 指定的 JSON 文件',
        )
        parser.add_argument(
            '--testset', type=str, default='',
            help='已有测试集 JSON 路径(配合 --skip-generate 使用)',
        )

    def handle(self, *args, **options):
        # 延迟导入,确保 Django 环境已就绪
        from apps.analytics.ragas_pipeline import run_full_pipeline, load_testset

        testset_size = options['testset_size']
        limit_docs = options['limit_docs']
        root_type = options['root_type'] or None
        model = options['model'] or None
        output_dir = options['output_dir']

        samples = None
        if options['skip_generate']:
            if not options['testset']:
                self.stderr.write(self.style.ERROR(
                    '--skip-generate 需配合 --testset <path> 使用'
                ))
                return
            if not os.path.exists(options['testset']):
                self.stderr.write(self.style.ERROR(
                    f'测试集文件不存在: {options["testset"]}'
                ))
                return
            samples = load_testset(options['testset'])
            self.stdout.write(f'复用测试集: {options["testset"]}, 样本数={len(samples)}')

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'启动 Ragas 全自动评估: testset_size={testset_size}, '
            f'limit_docs={limit_docs}, root_type={root_type or "ALL"}, '
            f'model={model or "(default)"}'
        ))

        try:
            result = run_full_pipeline(
                testset_size=testset_size,
                limit_docs=limit_docs,
                root_type=root_type,
                model=model,
                output_dir=output_dir,
                samples=samples,
            )
        except Exception as e:
            logger.exception('[RagasEval] 流水线执行失败')
            self.stderr.write(self.style.ERROR(f'评估失败: {e}'))
            return

        # 输出汇总到终端
        self.stdout.write(self.style.SUCCESS('\n========== 评估完成 =========='))
        self.stdout.write(f'测试集 ID: {result["testset_id"]}')
        self.stdout.write(f'样本数: {len(result["samples"])}')
        self.stdout.write('指标均值:')
        for k, v in result['summary'].items():
            self.stdout.write(f'  {k}: {v if v is not None else "N/A"}')
        self.stdout.write('报告文件:')
        for k, p in result['report_paths'].items():
            self.stdout.write(f'  {k}: {p}')
