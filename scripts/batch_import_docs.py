#!/usr/bin/env python3
"""
批量导入文档脚本（双阶段导入）

功能：从 scripts/upload/ 目录下批量导入文档到知识库，按目录结构自动映射节点

使用方法：
    # 默认：私有可见，超级管理员上传
    python scripts/batch_import_docs.py

    # 部门可见
    python scripts/batch_import_docs.py --visibility department --department-code R&D

    # 部门可见，指定上传者
    python scripts/batch_import_docs.py --visibility department --department-code R&D --owner user1

    # 团队可见
    python scripts/batch_import_docs.py --visibility team --team-code RAG-PROJ

    # 所有人可见
    python scripts/batch_import_docs.py --visibility public

工作原理：
    阶段一（脚本）：扫描目录 → 创建临时文件 → 发送到 Celery 队列
    阶段二（Celery）：验证 → 保存文件 → 创建记录 → 触发解析
    
    示例目录结构：
    scripts/upload/
        ├── 研发技术/
        │   ├── Django/
        │   │   └── tutorial.md
        │   └── Python/
        │       └── basics.md
        └── 行政办公/
            └── employee_handbook.md

支持的文件类型：.txt, .md, .docx, .pdf, .json, .xml, .csv, .xlsx
文件大小限制：100MB
"""
import os
import sys
import argparse
import uuid
import shutil
from pathlib import Path

# 当前文件位于 scripts/batch_import_docs.py，需向上两级到项目根
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import django
from django.conf import settings

# 配置 Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'rag_project.settings')
django.setup()

from apps.knowledge.models import KnowledgeNode
from apps.users.models import User, Team
from apps.knowledge.tasks import batch_import_single_file

# 文件大小限制：100MB
MAX_FILE_SIZE = 100 * 1024 * 1024


def find_or_create_node(node_name, parent_node=None):
    """查找或创建节点：存在则直接复用，不存在则创建"""
    query = KnowledgeNode.objects.filter(
        name=node_name,
        is_deleted=False
    )
    
    if parent_node:
        # 有父节点：只在父节点下查找同名子节点
        query = query.filter(parent=parent_node)
        existing = query.first()
    else:
        # 无父节点（第一层目录）：查找所有同名节点，优先选择深度最小的
        existing = query.order_by('depth').first()
    
    if existing:
        print(f"[REUSE] 节点: {node_name} (id={existing.id}, depth={existing.depth})")
        return existing
    
    # 创建新节点
    if parent_node:
        node_type = 'folder'
        depth = parent_node.depth + 1
        path = parent_node.path + f"{str(parent_node.id).zfill(4)}/"
        root_type = parent_node.root_type
    else:
        # 创建根节点：使用默认的 root_type
        node_type = 'root'
        depth = 0
        path = f"/{str(uuid.uuid4().hex[:4])}/"
        existing_root = KnowledgeNode.objects.filter(node_type='root', is_deleted=False).first()
        root_type = existing_root.root_type if existing_root else 'company_doc'
    
    node = KnowledgeNode.objects.create(
        parent=parent_node,
        root_type=root_type,
        node_type=node_type,
        name=node_name,
        path=path,
        depth=depth,
    )
    print(f"[CREATE] 节点: {node_name} (id={node.id})")
    return node


def list_available_nodes():
    """列出所有可用的根节点和子节点"""
    print("\n" + "=" * 60)
    print("可用节点列表")
    print("=" * 60)
    
    root_nodes = KnowledgeNode.objects.filter(node_type='root', is_deleted=False).order_by('id')
    for root in root_nodes:
        print(f"\n[{root.id}] {root.name}")
        
        def print_children(node, prefix="  ├─"):
            children = node.children.filter(is_deleted=False).order_by('order_no', 'name')
            for i, child in enumerate(children):
                connector = "  └─" if i == len(children) - 1 else "  ├─"
                print(f"{prefix} [{child.id}] {child.name}")
                if child.children.filter(is_deleted=False).exists():
                    print_children(child, prefix.replace("├", "│").replace("└", " ") + connector)
        
        print_children(root)


def list_available_departments():
    """列出所有可用的部门"""
    from apps.users.models import Department
    print("\n" + "=" * 60)
    print("可用部门列表")
    print("=" * 60)
    
    depts = Department.objects.filter(is_deleted=False, parent__isnull=True).order_by('sort_order')
    for dept in depts:
        print(f"\n[{dept.id}] {dept.name} (code={dept.code or '-'})")
        
        def print_sub_depts(node, prefix="  ├─"):
            children = node.children.filter(is_deleted=False).order_by('sort_order')
            for i, child in enumerate(children):
                connector = "  └─" if i == len(children) - 1 else "  ├─"
                print(f"{prefix} [{child.id}] {child.name} (code={child.code or '-'})")
                if child.children.filter(is_deleted=False).exists():
                    print_sub_depts(child, prefix.replace("├", "│").replace("└", " ") + connector)
        
        print_sub_depts(dept)


def import_documents(args):
    """执行批量导入（阶段一：扫描 + 发送到 Celery）"""
    # 验证参数
    upload_dir = Path(args.upload_dir)
    if not upload_dir.exists():
        print(f"[ERROR] 上传目录不存在: {upload_dir}")
        return 1
    
    # 获取上传者
    if args.owner:
        try:
            owner = User.objects.get(username=args.owner)
        except User.DoesNotExist:
            print(f"[ERROR] 上传者不存在: {args.owner}")
            return 1
    else:
        from apps.users.models import UserRoleRel, Role
        sa_role = Role.objects.filter(role_key='super_admin').first()
        if sa_role:
            sa_user_ids = UserRoleRel.objects.filter(role=sa_role).values_list('user_id', flat=True)
            owner = User.objects.filter(id__in=sa_user_ids, is_deleted=False).first()
        else:
            owner = None
        
        if not owner:
            print("[ERROR] 未找到超级管理员，请指定 --owner 参数")
            return 1
    
    print(f"[INFO] 上传者: {owner.username} ({owner.real_name})")
    
    # 解析可见范围
    visibility_map = {'private': 1, 'department': 2, 'team': 3, 'public': 4}
    visibility = visibility_map.get(args.visibility, 1)
    print(f"[INFO] 可见范围: {args.visibility} (level={visibility})")
    
    # 获取团队ID
    owner_team_id = None
    if visibility == 3 and args.team_code:
        try:
            team = Team.objects.get(code=args.team_code)
            owner_team_id = team.id
            print(f"[INFO] 目标团队: {team.name} (code={team.code})")
        except Team.DoesNotExist:
            print(f"[ERROR] 团队不存在: {args.team_code}")
            return 1
    
    # 处理部门参数
    if args.department_code:
        from apps.users.models import Department
        try:
            dept = Department.objects.get(code=args.department_code, is_deleted=False)
            print(f"[INFO] 目标部门: {dept.name} (code={dept.code}, id={dept.id})")
            
            if owner.department_id != dept.id:
                print(f"[WARNING] 上传者 {owner.username} 不在部门 {dept.name} 下")
            
            if visibility == 2:
                team = Team.objects.filter(department=dept, is_deleted=False).first()
                if team:
                    owner_team_id = team.id
                    print(f"[INFO] 目标团队: {team.name}")
        except Department.DoesNotExist:
            print(f"[ERROR] 部门不存在: {args.department_code}")
            return 1
    
    # 收集所有文件
    supported_extensions = ('.txt', '.md', '.markdown', '.docx', '.doc', '.pdf',
                           '.json', '.xml', '.csv', '.xlsx', '.xls',
                           '.ppt', '.pptx', '.wps', '.et', '.dps')
    files_to_import = []
    
    for root, dirs, files in os.walk(upload_dir):
        for filename in files:
            if filename.lower().endswith(supported_extensions):
                filepath = Path(root) / filename
                files_to_import.append(filepath)
    
    if not files_to_import:
        print(f"[WARNING] 未找到支持的文件类型，支持: {', '.join(supported_extensions)}")
        return 0
    
    print(f"[INFO] 共找到 {len(files_to_import)} 个文件")
    
    # 构建目录到节点的映射
    dir_node_map = {}
    
    for filepath in files_to_import:
        file_dir = filepath.parent
        rel_dir = file_dir.relative_to(upload_dir)
        
        parent = None
        current_parts = []
        
        for part in rel_dir.parts:
            current_parts.append(part)
            current_local_dir = upload_dir / Path(*current_parts)
            
            if current_local_dir not in dir_node_map:
                node = find_or_create_node(part, parent)
                dir_node_map[current_local_dir] = node
                parent = node
            else:
                parent = dir_node_map[current_local_dir]
        
        # 处理上传目录下的文件
        if file_dir == upload_dir and file_dir not in dir_node_map:
            root_node = KnowledgeNode.objects.filter(
                node_type='root', is_deleted=False
            ).first()
            if not root_node:
                root_node = find_or_create_node('文档')
            dir_node_map[file_dir] = root_node
    
    # 创建临时目录
    temp_dir = Path(settings.BASE_DIR) / 'temp' / 'batch_import'
    temp_dir.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] 临时目录: {temp_dir}")
    
    # 逐个发送到 Celery 队列
    queued_count = 0
    skipped_count = 0
    
    for filepath in files_to_import:
        try:
            filename = filepath.name
            file_size = filepath.stat().st_size
            
            # 文件大小检查
            if file_size > MAX_FILE_SIZE:
                print(f"[SKIP] 文件大小超过限制（{file_size/(1024*1024):.1f}MB > 100MB）: {filename}")
                skipped_count += 1
                continue
            
            # 确定目标节点
            file_dir = filepath.parent
            current_dir = file_dir
            node = None
            
            while current_dir not in dir_node_map:
                if current_dir == upload_dir:
                    break
                current_dir = current_dir.parent
            
            if current_dir in dir_node_map:
                node = dir_node_map[current_dir]
            else:
                root_node = KnowledgeNode.objects.filter(
                    node_type='root', is_deleted=False
                ).first()
                if not root_node:
                    root_node = find_or_create_node('文档')
                node = root_node
            
            # 复制到临时目录
            temp_filename = f"{uuid.uuid4().hex}_{filename}"
            temp_file_path = temp_dir / temp_filename
            shutil.copy2(filepath, temp_file_path)
            
            # 发送到 Celery 队列
            batch_import_single_file.delay(
                str(temp_file_path),
                node.id,
                owner.id,
                visibility,
                owner_team_id,
                filename
            )
            
            print(f"[QUEUED] {filename} -> {node.name} (temp: {temp_file_path.name})")
            queued_count += 1
            
        except Exception as e:
            print(f"[FAIL] 发送失败: {filename} - {str(e)}")
            skipped_count += 1
    
    # 输出统计
    print("\n" + "=" * 60)
    print("批量导入任务已提交")
    print("=" * 60)
    print(f"  总计: {len(files_to_import)}")
    print(f"  已入队: {queued_count}")
    print(f"  跳过: {skipped_count}")
    print("\n[INFO] 任务已发送到 Celery 队列，等待异步处理...")
    print(f"[INFO] 失败日志将记录到: {settings.BASE_DIR}/logs/batch_import_failed.log")
    
    return 0


def main():
    parser = argparse.ArgumentParser(description='批量导入文档到知识库（按目录结构映射节点）')
    
    # 可见范围参数
    parser.add_argument('--visibility', type=str, default='private',
                       choices=['private', 'department', 'team', 'public'],
                       help='可见范围：private(私有)/department(部门)/team(团队)/public(公开)')
    parser.add_argument('--team-code', type=str, help='团队可见时的团队编码')
    parser.add_argument('--department-code', type=str, help='部门编码（部门可见时必填）')
    
    # 上传者参数
    parser.add_argument('--owner', type=str, help='上传者用户名（默认超级管理员）')
    
    # 其他参数
    parser.add_argument('--upload-dir', type=str, default='scripts/upload',
                       help='上传目录（默认：scripts/upload）')
    parser.add_argument('--list-nodes', action='store_true', help='列出所有可用的节点')
    parser.add_argument('--list-departments', action='store_true', help='列出所有可用的部门')
    
    args = parser.parse_args()
    
    # 参数验证
    if args.visibility == 'team' and not args.team_code:
        parser.error('团队可见时必须指定 --team-code 参数')
    if args.visibility == 'department' and not args.department_code:
        parser.error('部门可见时必须指定 --department-code 参数')
    
    # 列出节点
    if args.list_nodes:
        list_available_nodes()
        return 0
    
    # 列出部门
    if args.list_departments:
        list_available_departments()
        return 0
    
    # 执行导入
    return import_documents(args)


if __name__ == '__main__':
    sys.exit(main())
