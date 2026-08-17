<template>
  <div class="page-container admin-nodes-page">
    <!-- ===== 页头：所有登录用户可浏览文档；节点增删改仅管理员/团队组长可用（按钮显隐控制） ===== -->
    <div class="page-header">
      <div>
        <div class="page-title">知识库</div>
        <div class="page-desc">浏览知识库文档，按节点筛选；管理员可管理目录树节点</div>
      </div>
      <el-button v-if="canManageNodes()" type="primary" @click="openNodeModal">＋ 新增节点</el-button>
    </div>

    <!-- ===== 主体：左节点树 + 右节点详情 ===== -->
    <div class="page-body">
      <div class="node-manage">
      <!-- 节点树面板 -->
      <div class="node-tree-panel">
        <PanelHeader title-class="panel-title">
          节点树
          <template #actions>
            <el-button link size="small" title="展开全部" @click="expandAllTree">⛶ 展开全部</el-button>
          </template>
        </PanelHeader>
        <div class="panel-body" v-loading="treeLoading">
          <el-tree
            ref="treeRef"
            :data="nodeTree"
            node-key="id"
            :props="{ label: 'name', children: 'children' }"
            :expand-on-click-node="false"
            highlight-current
            @node-click="onTreeNodeClick"
          >
            <template #default="{ data }">
              <span class="tree-node-item">
                <span class="tree-node-icon">{{ nodeIcon(data) }}</span>
                <span class="tree-node-label">{{ data.name }}</span>
                <span class="tree-node-count">{{ data.document_count || 0 }}</span>
              </span>
            </template>
          </el-tree>
          <el-empty v-if="!treeLoading && !nodeTree.length" description="暂无节点，请先创建" :image-size="60" />
        </div>
      </div>

      <!-- 节点详情面板 -->
      <div class="node-detail-panel">
        <el-empty v-if="!selectedNode" description="请在左侧选择或创建一个节点" :image-size="80" />
        <template v-else>
          <!-- 头部：名称 + 操作 -->
          <div class="nd-head">
            <div class="nd-head-left">
              <div class="nd-title">
                <span class="nd-icon">{{ nodeIcon(selectedNode) }}</span>
                <span class="nd-name">{{ selectedNode.name }}</span>
              </div>
              <div class="text-sub text-xs nd-meta">节点 ID：node-{{ selectedNode.id }} · 路径：{{ nodePathText(selectedNode) }}</div>
            </div>
            <!-- 编辑对全部节点开放（名称/可见范围变更走审批，ORG/ROOT 名称不可改）；
                 删除仅 FOLDER 文件夹可操作（ROOT/ORG 由系统/组织架构管理） -->
            <div class="nd-actions" v-if="canEditNode(selectedNode)">
              <el-button size="small" @click="openNodeEditModal(selectedNode.id)">📝 编辑</el-button>
              <el-button v-if="selectedNode.node_kind === 'FOLDER'" size="small" type="danger" @click="deleteNode(selectedNode.id)">🗑 删除文件夹</el-button>
            </div>
          </div>

          <!-- 统计卡片 -->
          <div class="nd-stats">
            <div class="stat-card nd-doc-card" title="点击查看文档列表" @click="viewNodeDocs(selectedNode.id)">
              <div class="stat-card-label">📄 文档总数</div>
              <div class="stat-card-row">
                <span class="stat-card-value">{{ (selectedNode.document_count || 0).toLocaleString() }}</span>
                <span class="stat-card-link">点击查看 →</span>
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-card-label">📂 子节点数</div>
              <div class="stat-card-value">{{ (selectedNode.children_count || 0).toLocaleString() }}</div>
            </div>
            <div class="stat-card">
              <div class="stat-card-label">🏷️ 节点类型</div>
              <div class="stat-card-value">{{ NODE_KIND_LABELS[selectedNode.node_kind] || selectedNode.node_type || '—' }}</div>
            </div>
          </div>

          <!-- 基础信息 -->
          <div class="nd-info-card">
            <div class="card-title">基础信息</div>
            <div class="nd-info-grid">
              <div>
                <div class="text-sub text-xs">节点名称</div>
                <div class="nd-info-value">{{ selectedNode.name }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">根类型</div>
                <div class="nd-info-value">{{ rootTypeName(selectedNode) }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">创建人</div>
                <div class="nd-info-value">{{ selectedNode.created_by_name || '—' }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">创建时间</div>
                <div class="nd-info-value">{{ formatDate(selectedNode.created_at) }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">最后更新</div>
                <div class="nd-info-value">{{ formatDate(selectedNode.updated_at) }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">上级节点</div>
                <div class="nd-info-value">{{ selectedNode.parent_id ? '节点#' + selectedNode.parent_id : '（根节点）' }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">可见范围</div>
                <div class="nd-info-value">{{ nodeVisLabel(selectedNode) }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">排序号</div>
                <div class="nd-info-value">{{ selectedNode.order_no || 0 }}</div>
              </div>
              <div>
                <div class="text-sub text-xs">深度</div>
                <div class="nd-info-value">第{{ selectedNode.depth }}层</div>
              </div>
            </div>
            <!-- 描述（合并在基础信息卡片内） -->
            <div v-if="selectedNode.description" class="nd-desc">
              <div class="text-sub text-xs nd-desc-title">描述</div>
              <div class="nd-desc-text">{{ selectedNode.description }}</div>
            </div>
          </div>
        </template>
      </div>
      </div>
    </div>

    <!-- ============ 文档列表弹窗（BaseDialog 骨架：默认 80% 宽高 + 最小尺寸 + 屏幕居中） ============ -->
    <BaseDialog v-model="docListVisible" @open="onDocListOpened" @closed="onDocListClosed">
      <template #header>
        <span>文档列表</span>
        <span class="doc-count-sub text-sub text-xs">（共 {{ docListTotal }} 条）</span>
      </template>
      <!-- 页面自行控制 body 内部布局：筛选栏/分页固定高度，表格撑满剩余空间内部滚动 -->
      <div class="doc-list-body" ref="docListBodyRef">
      <!-- 筛选栏 -->
      <div class="doc-filter-bar">
        <el-input v-model="docSearch" placeholder="🔍 搜索文件名/标题" clearable style="width: 220px" @input="onDocSearchInput" @clear="onDocSearchInput" />
        <el-select v-model="docStatusFilter" placeholder="全部状态" style="width: 150px" @change="loadDocList(1)">
          <el-option label="全部状态" value="" />
          <el-option v-for="opt in DOC_STATUS_FILTERS" :key="opt.value" :label="opt.text" :value="opt.value" />
        </el-select>
        <el-select v-model="docVisFilter" placeholder="全部可见范围" style="width: 130px" @change="loadDocList(1)">
          <el-option label="全部可见范围" value="" />
          <el-option label="团队" value="team" />
          <el-option label="部门" value="dept" />
          <el-option label="公开" value="public" />
        </el-select>
        <el-select v-model="docTypeFilter" placeholder="全部类型" style="width: 120px" @change="loadDocList(1)">
          <el-option label="全部类型" value="" />
          <el-option v-for="opt in DOC_TYPE_FILTERS" :key="opt.value" :label="opt.text" :value="opt.value" />
        </el-select>
        <el-button size="small" type="primary" @click="loadDocList(1)">查询</el-button>
        <!-- 含旧版本：勾选后列表展示全部版本（含被新版本替换的非活跃版本），用于回溯与切换 -->
        <el-checkbox v-model="docShowAll" @change="loadDocList(1)">含旧版本</el-checkbox>
      </div>
      <el-table :data="docListDocs" v-loading="docListLoading" size="default" :height="docTableHeight">
        <el-table-column label="文件名" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="flex items-center gap-6">
              <span>{{ fileTypeIcon(row.file_type) }}</span>
              <span class="doc-file-name">{{ row.file_name }}</span>
              <!-- 活跃标记：有多版本时标注当前生效版本；非活跃版本（?version=all 可见）标注旧版本 -->
              <template v-if="row.version_count > 1">
                <el-tag v-if="row.is_active" type="success" size="small" effect="plain">活跃</el-tag>
                <el-tag v-else type="info" size="small" effect="plain">旧版本</el-tag>
              </template>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag type="info" size="small" effect="plain">{{ row.file_type || '-' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上传人" width="120" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sub">{{ row.owner_name || '-' }}</span></template>
        </el-table-column>
        <el-table-column label="归属团队" width="120" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sub">{{ getDocTeamName(row) }}</span></template>
        </el-table-column>
        <el-table-column label="可见范围" width="90">
          <template #default="{ row }">
            <el-tag :type="visTagType(row.visible_scope)" size="small" effect="plain">{{ visTagText(row.visible_scope) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag :type="statusTag(row).type" size="small" effect="plain">{{ statusTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="130">
          <template #default="{ row }">
            <span class="text-sub" :title="formatDate(row.created_at)">{{ formatDateShort(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="240">
          <template #default="{ row }">
            <!-- 预览：支持预览的文件类型且用户有阅读权限才展示（无权限时隐藏，后端仍会二次校验） -->
            <el-button v-if="isPreviewableFileType(row.file_type) && row.can_read !== false" link type="primary" size="small" @click="openPreview(row.id)">预览</el-button>
            <!-- 版本切换：同组存在多个版本时展示入口（活跃/旧版本均可打开版本历史弹窗） -->
            <el-button v-if="row.version_count > 1" link type="primary" size="small" @click="showVersionModal(row.id)">版本</el-button>
            <template v-if="row.is_owner || row.is_manager">
              <el-button link type="primary" size="small" @click="openAccessModal(row.id)">访问管理</el-button>
              <el-button link type="primary" size="small" @click="openVisModal(row.id)">设置</el-button>
              <el-button link type="danger" size="small" @click="deleteDoc(row.id)">删除</el-button>
            </template>
            <el-button v-else link type="warning" size="small" @click="openReqModal(row.id)">申请权限</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无文档" :image-size="60" />
        </template>
      </el-table>
      <!-- 分页：后端按 page_size 切片；切换每页条数时重置回第 1 页 -->
      <AppPagination
        class="doc-pagination"
        layout="total, sizes, prev, pager, next"
        :total="docListTotal"
        :page-size="docListPageSize"
        :page="docListPage"
        :page-sizes="[10, 20, 50]"
        @page-change="onDocPageChange"
        @size-change="onPageSizeChange"
      />
      </div>
    </BaseDialog>

    <!-- ============ 编辑文档设置弹窗（可见范围调整） ============ -->
    <el-dialog v-model="visVisible" title="编辑文档设置" width="600px" :close-on-click-modal="false">
      <template v-if="visDoc">
        <!-- 文档归属信息卡片 -->
        <div class="doc-ownership-card">
          <div class="doc-ownership-header">
            <span class="doc-ownership-icon">📄</span>
            <span class="doc-ownership-name">{{ visDoc.file_name || visDoc.title || '—' }}</span>
          </div>
          <div class="doc-ownership-meta">
            <div class="doc-ownership-row">
              <span class="doc-ownership-label">归属</span>
              <span class="doc-ownership-value">{{ visOwnership }}</span>
            </div>
            <div class="doc-ownership-row">
              <span class="doc-ownership-label">上传者</span>
              <span class="doc-ownership-value">{{ visDoc.owner_name || '—' }}</span>
            </div>
            <div class="doc-ownership-row">
              <span class="doc-ownership-label">当前可见</span>
              <span class="doc-ownership-value">{{ SCOPE_LABELS[visOldScope] || visOldScope || '—' }}</span>
            </div>
          </div>
        </div>

        <div class="form-section-title">调整可见范围</div>
        <el-select v-model="visSelectValue" style="width: 100%" @change="onDocVisChange">
          <el-option v-for="opt in visOptions" :key="opt.value" :label="opt.text" :value="opt.value" />
        </el-select>

        <!-- 扩大范围提示：扩大可见范围需两位管理员先后审批 -->
        <div v-if="showVisUpgradeHint" class="hint-warning">⚠️ 扩大可见范围需两位管理员先后审批</div>

        <!-- 缩小范围：选择目标团队/部门 -->
        <div v-if="showNarrowPanel" class="narrow-panel">
          <div class="multi-select-row">
            <el-select
              v-if="showNarrowDeptSelect"
              v-model="narrowDepts"
              multiple
              collapse-tags
              filterable
              placeholder="请选择部门"
              style="width: 230px"
              @change="onNarrowDeptChange"
            >
              <el-option v-for="d in visDeptList" :key="d.id" :label="d.name" :value="d.id" />
            </el-select>
            <el-select
              v-model="narrowTeams"
              multiple
              collapse-tags
              filterable
              :placeholder="narrowTeamPlaceholder"
              :disabled="!narrowDeptSelected"
              style="width: 230px"
            >
              <el-option v-for="t in narrowTeamOptions" :key="t.id" :label="t.name" :value="t.id" />
            </el-select>
          </div>
          <div class="form-hint">{{ visNarrowHint }}</div>
        </div>
      </template>
      <template #footer>
        <el-button @click="visVisible = false">取消</el-button>
        <el-button type="primary" @click="saveDocVis">保存</el-button>
      </template>
    </el-dialog>

    <!-- ============ 申请权限弹窗 ============ -->
    <el-dialog v-model="reqVisible" title="申请文档权限" width="640px" :close-on-click-modal="false">
      <!-- 文档归属信息卡片：让申请人在提交前确认申请对象 -->
      <div class="doc-ownership-card">
        <div class="doc-ownership-header">
          <span class="doc-ownership-icon">📄</span>
          <span class="doc-ownership-name">{{ reqDoc.file_name || reqDoc.title || '—' }}</span>
        </div>
        <div class="doc-ownership-meta">
          <div class="doc-ownership-row">
            <span class="doc-ownership-label">上传者</span>
            <span class="doc-ownership-value">{{ reqDoc.owner_name || '—' }}</span>
          </div>
          <div class="doc-ownership-row">
            <span class="doc-ownership-label">归属团队</span>
            <span class="doc-ownership-value">{{ getDocTeamName(reqDoc) }}</span>
          </div>
          <div class="doc-ownership-row">
            <span class="doc-ownership-label">当前可见</span>
            <span class="doc-ownership-value">{{ SCOPE_LABELS[reqDoc.visible_scope] || reqDoc.visible_scope || '—' }}</span>
          </div>
        </div>
      </div>
      <div class="form-section-title">申请信息</div>
      <div class="form-item mb-16">
        <label class="form-label">申请类型</label>
        <el-select v-model="reqForm.type" style="width: 100%">
          <el-option label="读取（预览/对话检索）" value="read" />
          <el-option label="下载" value="download" />
          <el-option label="分享" value="share" />
        </el-select>
      </div>
      <div class="form-item mb-12">
        <label class="form-label">申请理由</label>
        <el-input v-model="reqForm.reason" type="textarea" :rows="3" placeholder="简要说明申请原因" />
      </div>
      <div class="hint-warning">⚠️ 申请提交后需管理员审批，审批通过后自动生效</div>
      <template #footer>
        <el-button @click="reqVisible = false">取消</el-button>
        <el-button type="primary" @click="submitRequest">提交申请</el-button>
      </template>
    </el-dialog>

    <!-- ============ 访问管理弹窗（查看/撤销授权 + 审批申请，BaseDialog 骨架） ============ -->
    <BaseDialog v-model="accessVisible" title="访问管理">
      <!-- 页面自行控制 body 内部布局：整体内部滚动 -->
      <div class="access-body">
      <div class="form-section-title">已授权用户</div>
      <el-table :data="grantRows" v-loading="grantsLoading" size="small">
        <el-table-column label="用户" min-width="140">
          <template #default="{ row }">
            <el-tag v-if="row.kind === 'dept'" type="primary" effect="plain">🏢 {{ row.name }}</el-tag>
            <el-tag v-else-if="row.kind === 'team'" type="primary" effect="plain">👥 {{ row.name }}</el-tag>
            <span v-else>{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="权限" width="90">
          <template #default="{ row }"><el-tag type="info" size="small" effect="plain">{{ row.action }}</el-tag></template>
        </el-table-column>
        <el-table-column label="来源" width="100">
          <template #default="{ row }"><span class="text-sub">{{ row.source }}</span></template>
        </el-table-column>
        <el-table-column label="到期" width="130">
          <template #default="{ row }"><span class="text-sub">{{ row.expires }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.active" type="success" size="small" effect="plain">有效</el-tag>
            <el-tag v-else type="danger" size="small" effect="plain">已撤销</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button v-if="row.revocable" link type="danger" size="small" @click="revokeGrant(row)">撤销</el-button>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无授权记录" :image-size="50" />
        </template>
      </el-table>

      <div class="form-section-title">待审批申请</div>
      <el-table :data="pendingReqs" v-loading="reqsLoading" size="small">
        <el-table-column label="申请人" min-width="120">
          <template #default="{ row }">{{ row.requester_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }"><el-tag type="info" size="small" effect="plain">{{ GRANT_TYPE_MAP[row.access_type] || row.access_type }}</el-tag></template>
        </el-table-column>
        <el-table-column label="理由" min-width="200" show-overflow-tooltip>
          <template #default="{ row }"><span class="text-sub">{{ row.reason || '-' }}</span></template>
        </el-table-column>
        <el-table-column label="申请时间" width="140">
          <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button link type="success" size="small" @click="approveReq(row.id)">批准</el-button>
            <el-button link type="danger" size="small" @click="rejectReq(row.id)">驳回</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无待审批申请" :image-size="50" />
        </template>
      </el-table>
      </div>
    </BaseDialog>

    <!-- ============ 新增/编辑节点弹窗 ============ -->
    <el-dialog v-model="nodeModalVisible" :title="nodeModalTitle" width="560px" :close-on-click-modal="false">
      <!-- ===== 节点归属 ===== -->
      <div class="form-section-title">节点归属</div>
      <div class="form-item">
        <label class="form-label">上级节点 <span class="required">*</span></label>
        <el-select v-model="nodeForm.parent" style="width: 100%" :disabled="nodeParentDisabled" filterable @change="onNodeParentChange">
          <el-option v-for="opt in parentOptions" :key="opt.id" :label="opt.name" :value="opt.id" />
        </el-select>
        <div class="form-hint">{{ parentHint }}</div>
      </div>
      <div class="form-item">
        <label class="form-label">可见范围</label>
        <el-select v-model="nodeForm.visibility" style="width: 100%" :disabled="nodeVisibilityDisabled">
          <el-option label="继承父级（默认）" value="" />
          <el-option label="仅团队" value="TEAM_ONLY" />
          <el-option label="仅部门" value="DEPT_ONLY" />
          <el-option label="全局公开" value="PUBLIC" />
        </el-select>
        <div class="form-hint">{{ nodeVisHint }}</div>
      </div>

      <!-- ===== 基本信息 ===== -->
      <div class="form-section-title">基本信息</div>
      <div class="form-row">
        <div class="form-item flex-1">
          <label class="form-label">节点名称 <span class="required">*</span></label>
          <el-input v-model="nodeForm.name" :disabled="nodeNameDisabled" maxlength="128" placeholder="例如：产品文档、后端服务" />
        </div>
        <div class="form-item order-input">
          <label class="form-label">显示顺序</label>
          <el-input-number v-model="nodeForm.orderNo" :min="0" :max="9999" controls-position="right" style="width: 130px" />
        </div>
      </div>
      <div class="form-hint node-basic-hint">{{ nodeBasicHint }}</div>
      <div class="form-item">
        <label class="form-label">节点描述</label>
        <el-input v-model="nodeForm.desc" type="textarea" :rows="3" placeholder="简要描述该节点的用途和包含的内容范围" />
      </div>
      <template #footer>
        <el-button @click="nodeModalVisible = false">取消</el-button>
        <el-button type="primary" @click="saveNode">保存</el-button>
      </template>
    </el-dialog>

    <!-- ============ 文档预览弹窗（公共组件 DocPreviewDialog，与 Chat/Upload 共用） ============ -->
    <DocPreviewDialog v-model="previewVisible" :doc-id="previewDocId" :initial-page="previewInitialPage" />

    <!-- ============ 版本历史弹窗 ============ -->
    <el-dialog v-model="versionVisible" :title="versionTitle" width="760px">
      <el-table :data="versionList" v-loading="versionLoading" size="small">
        <el-table-column label="文件名" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.title || '-' }}</template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }">{{ row.version_tag || ('v' + row.version) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small" effect="plain">活跃</el-tag>
            <el-tag v-else type="info" size="small" effect="plain">旧版本</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="处理状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTag(row).type" size="small" effect="plain">{{ statusTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="90">
          <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="上传时间" width="120">
          <template #default="{ row }">{{ formatDateShort(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <template v-if="row.can_read !== false && isPreviewableFileType(row.file_type)">
              <el-button link type="primary" size="small" @click="openPreview(row.id)">预览</el-button>
              <el-button v-if="!row.is_active && row.is_owner" link type="primary" size="small" @click="setVersionActive(row.id, versionDocId)">设为活跃</el-button>
            </template>
            <template v-else-if="row.is_active">
              <span class="text-sub text-xs">当前</span>
            </template>
            <template v-else-if="row.is_owner">
              <el-button link type="primary" size="small" @click="setVersionActive(row.id, versionDocId)">设为活跃</el-button>
            </template>
            <template v-else>
              <span class="text-sub text-xs">仅上传者可切换</span>
            </template>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无版本记录" :image-size="60" />
        </template>
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import {
  formatDate, formatDateShort, formatFileSize, errMsg,
  isPreviewableFileType, pipelineStatus
} from '../utils/format'
import { visTagType, visTagText, fileTypeIcon } from '../utils/labels'
import { debounce } from '../utils/debounce'
import { usePagination } from '../composables/usePagination'
import { useConfirm } from '../composables/useConfirm'
import { useKnowledgeTree } from '../composables/useKnowledgeTree'
import PanelHeader from '../components/base/PanelHeader.vue'
import DocPreviewDialog from '../components/doc-preview/DocPreviewDialog.vue'
import BaseDialog from '../components/base/BaseDialog.vue'
import AppPagination from '../components/base/AppPagination.vue'

const userStore = useUserStore()
// 二次确认弹窗统一封装
const { confirm } = useConfirm()

/* ==========================================================
   常量（与旧 admin-nodes.js 保持一致）
   ========================================================== */
const NODE_API = '/api/v1/knowledge/nodes'
const DOC_API = '/api/v1/knowledge/documents'

const NODE_KIND_LABELS = { ROOT: '根节点', ORG: '组织节点', FOLDER: '文件夹' }
const SCOPE_LABELS = { team: '团队', dept: '部门', public: '全公司公开' }
const SCOPE_ORDER = { team: 0, dept: 1, public: 2 }
const ACCESS_ACTION_MAP = { read: '读取', download: '下载', share: '分享', edit: '编辑', export: '导出' }
const ACCESS_SOURCE_MAP = { direct: '直接', share: '分享', request: '申请' }
const GRANT_TYPE_MAP = { read: '读取', download: '下载', share: '分享' }

// 文档列表状态筛选（与旧 HTML 下拉一致）
const DOC_STATUS_FILTERS = [
  { value: 'pending', text: '等待解析' },
  { value: 'parsing', text: '解析中' },
  { value: 'chunking', text: '切片中' },
  { value: 'embedding', text: '向量构建中' },
  { value: 'embedding_failed', text: '向量构建失败' },
  { value: 'failed', text: '解析失败' },
  { value: 'graph_pending', text: '等待图谱构建' },
  { value: 'graph_extracting', text: '图谱构建中' },
  { value: 'graph_failed', text: '图谱构建失败' },
  { value: 'wiki_pending', text: '等待Wiki生成' },
  { value: 'wiki_extracting', text: 'Wiki生成中' },
  { value: 'wiki_failed', text: 'Wiki生成失败' },
  { value: 'done', text: '已完成' }
]

const DOC_TYPE_FILTERS = [
  { value: 'pdf', text: 'PDF' },
  { value: 'docx', text: 'Word' },
  { value: 'markdown', text: 'Markdown' },
  { value: 'txt', text: 'TXT' },
  { value: 'code', text: '代码' },
  { value: 'config', text: '配置' }
]

/* ==========================================================
   节点树共享逻辑（树加载/选中/图标/路径 + 团队组长权限判断）
   抽取自 useKnowledgeTree composable，节点弹窗/文档列表/可见范围设置等共用；
   onSelect 回调在选中节点时同步文档列表的节点筛选
   ========================================================== */
const {
  nodeTree, selectedNodeId, selectedNode, treeRef, treeLoading,
  loadRootTypes, loadTree, expandAllTree, onTreeNodeClick,
  findNodeById, isTeamLeader, canManageNodes, getTeamLeaderTeamNodeIds, canEditNode,
  nodeIcon, nodePathText, rootTypeName, nodeVisLabel,
} = useKnowledgeTree({ onSelect: setDocNodeFilter })

/* ==========================================================
   新增/编辑节点弹窗
   ========================================================== */
const nodeModalVisible = ref(false)
const nodeModalTitle = ref('新增文件夹')
const nodeForm = reactive({ id: null, parent: '', name: '', desc: '', orderNo: 0, visibility: '' })
const parentOptions = ref([])
const nodeParentDisabled = ref(false)
const nodeNameDisabled = ref(false)
const nodeVisibilityDisabled = ref(false)
const parentHint = ref('')
const nodeVisHint = ref('')
const nodeBasicHint = ref('')

function openNodeModal() {
  nodeForm.id = null
  nodeForm.parent = ''
  nodeForm.name = ''
  nodeForm.desc = ''
  nodeForm.orderNo = 0
  nodeForm.visibility = ''
  nodeModalTitle.value = '新增文件夹'
  // 编辑弹窗会禁用名称/可见范围输入，新增时必须复位
  nodeNameDisabled.value = false
  nodeVisibilityDisabled.value = false
  nodeBasicHint.value = '数字越小越靠前，同级节点按此值升序排列，默认为 0'

  const isTL = isTeamLeader()
  // 手动创建的一律是文件夹；所有用户都必选上级节点
  parentHint.value = isTL
    ? '必须选择一个团队范围内的上级节点'
    : '超管/文档管理员可在知识库根下创建文件夹（与部门同级）；其他角色选择自己范围内的上级节点'

  // 复用已加载的 nodeTree 构建父节点列表；编辑弹窗会禁用上级节点下拉，新增时必须复位为可交互
  buildParentOptions(null, () => {
    nodeParentDisabled.value = false
    // 按父节点类型初始化可见范围可用性（子文件夹仅做分类）
    onNodeParentChange()
  })
  nodeModalVisible.value = true
}

async function openNodeEditModal(id) {
  try {
    const node = await api.getJson(NODE_API + '/' + id + '/')
    const isSystemNode = node.node_kind !== 'FOLDER'
    // 子文件夹（父节点也是文件夹）：仅做分类，权限继承主文件夹
    const parentNode = node.parent_id ? findNodeById(node.parent_id) : null
    const isSubFolder = parentNode && parentNode.node_kind === 'FOLDER'

    nodeForm.id = node.id
    nodeForm.name = node.name
    nodeForm.desc = node.description || ''
    nodeForm.orderNo = node.order_no || 0
    nodeForm.visibility = node.visibility_level || ''
    nodeModalTitle.value = NODE_KIND_LABELS[node.node_kind] ? '编辑' + NODE_KIND_LABELS[node.node_kind] : '编辑节点'

    // 编辑时不允许修改父节点
    buildParentOptions(node.parent_id, () => {
      nodeParentDisabled.value = true
      // 系统节点（ROOT/ORG）名称由组织架构同步维护，不可修改
      if (isSystemNode) {
        nodeNameDisabled.value = true
        nodeBasicHint.value = '系统节点名称由组织架构同步维护，不可修改；显示顺序/描述可直接修改'
      } else {
        nodeNameDisabled.value = false
        nodeBasicHint.value = '修改名称/可见范围需走工单审批；显示顺序/描述可直接修改'
      }
      // 子文件夹：可见范围禁用并清空（权限继承主文件夹，不支持独立设置）
      if (isSubFolder) {
        nodeVisibilityDisabled.value = true
        nodeForm.visibility = ''
        nodeVisHint.value = '子文件夹仅做分类，权限继承主文件夹（' + parentNode.name + '）'
      } else {
        nodeVisibilityDisabled.value = false
        nodeVisHint.value = '未设置时继承上级节点可见范围；上传文档未指定可见范围时以此为准'
      }
      // 系统节点名称不可修改，提示不能使用"需走工单审批"文案，避免误导用户以为可以走工单改名
      parentHint.value = isSystemNode
        ? '系统节点名称不可修改；显示顺序/描述可直接修改，无需审批'
        : '修改名称/可见范围需走工单审批，审批通过后生效'
    })
    nodeModalVisible.value = true
  } catch (e) {
    ElMessage.error('加载节点详情失败: ' + errMsg(e, ''))
  }
}

/** 父节点变化时联动可见范围选择：子文件夹（父节点也是文件夹）仅做分类，权限继承主文件夹 */
function onNodeParentChange() {
  const parentNode = nodeForm.parent ? findNodeById(nodeForm.parent) : null
  const isSubFolder = !!parentNode && parentNode.node_kind === 'FOLDER'
  if (isSubFolder) {
    nodeVisibilityDisabled.value = true
    nodeForm.visibility = ''
    nodeVisHint.value = '子文件夹仅做分类，权限继承主文件夹（' + parentNode.name + '）'
  } else {
    nodeVisibilityDisabled.value = false
    nodeVisHint.value = '未设置时继承上级节点可见范围；上传文档未指定可见范围时以此为准'
  }
}

/** 从 nodeTree 构建可选父节点列表（复用已加载数据；若未加载则回退 API 请求） */
function buildParentOptions(selectedParentId, cb) {
  const options = []
  // 团队组长：只展示本团队范围内的节点
  let teamNodeIds = getTeamLeaderTeamNodeIds()
  let teamPaths = teamNodeIds.map(id => {
    const found = findNodeById(id)
    return found ? found.path : ''
  }).filter(Boolean)
  const isTL = isTeamLeader()

  function isInTeam(n) {
    if (!isTL) return true // 非组长不过滤
    for (let i = 0; i < teamPaths.length; i++) {
      const tp = teamPaths[i]
      // tp 本身以 / 结尾；子节点 path 以 tp 开头即为团队子树内
      if (n.path === tp || n.path.indexOf(tp) === 0) return true
    }
    return false
  }

  function flatten(items, depth) {
    items.forEach(n => {
      // 团队组长：只展示团队范围内的非叶子节点（不展开但允许遍历子节点，子节点可能在后代中匹配）
      if (isTL && !isInTeam(n)) {
        if (n.children && n.children.length) flatten(n.children, depth)
        return
      }
      if (n.node_type !== 'leaf') {
        const indent = '\u3000'.repeat(depth * 2)
        const prefix = depth > 0 ? '└ ' : ''
        options.push({ id: n.id, name: indent + prefix + n.name })
      }
      if (n.children && n.children.length) flatten(n.children, depth + 1)
    })
  }

  function renderOptions() {
    // 编辑模式：如果当前节点的父节点不在 options 中，手动添加
    if (selectedParentId && !options.some(o => o.id === selectedParentId)) {
      options.unshift({ id: selectedParentId, name: '(上级节点#' + selectedParentId + ')' })
    }
    parentOptions.value = options
    if (cb) cb()
  }

  if (nodeTree.value.length > 0) {
    flatten(nodeTree.value, 0)
    renderOptions()
  } else {
    // 树尚未加载完成，回退 API 请求（回退时也更新 nodeTree，确保 team leader 权限辅助可用）
    api.getJson(NODE_API + '/tree/').then(data => {
      const tree = data.tree || []
      if (isTL) {
        nodeTree.value = tree
        teamNodeIds = getTeamLeaderTeamNodeIds()
        teamPaths = teamNodeIds.map(id => {
          const found = findNodeById(id)
          return found ? found.path : ''
        }).filter(Boolean)
      }
      flatten(tree, 0)
      renderOptions()
    }).catch(() => {
      parentOptions.value = []
      if (cb) cb()
    })
  }
}

/** 保存节点（新增/编辑）；名称/可见范围变更走工单审批 */
async function saveNode() {
  const id = nodeForm.id
  const name = (nodeForm.name || '').trim()
  const desc = (nodeForm.desc || '').trim()
  const orderNo = parseInt(nodeForm.orderNo, 10) || 0
  const visibility = nodeForm.visibility

  if (!name) { ElMessage.warning('请输入节点名称'); return }
  // 文件夹必须选择上级节点
  if (!nodeForm.parent) { ElMessage.warning('文件夹必须选择上级节点'); return }

  const body = {
    node_type: 'folder',
    name,
    description: desc,
    order_no: orderNo
  }
  if (id) {
    // 编辑模式：PATCH 提交可见范围（空值表示继承父级，写回 null），变更走工单审批
    body.visibility_level = visibility || null
  } else if (visibility) {
    // 新建模式：初始可见范围直接生效，无需审批
    body.visibility_level = visibility
  }
  // 仅新建时发送 parent，编辑时不修改归属
  if (!id && nodeForm.parent) body.parent = nodeForm.parent

  try {
    if (id) await api.patchJson(NODE_API + '/' + id + '/', body)
    else await api.postJson(NODE_API + '/', body)
    ElMessage.success(id ? '节点已更新' : '节点已创建')
    selectedNodeId.value = null
    selectedNode.value = null
    // 等树刷新完再关闭弹窗，确保下次打开时父节点列表是最新的
    await loadTree()
    nodeModalVisible.value = false
  } catch (e) {
    // 403 + 审批提示 = 名称/可见范围变更已自动提交审批工单（非失败），以成功提示告知用户
    if (e && e.status === 403 && e.message && e.message.indexOf('已自动提交审批工单') !== -1) {
      ElMessage.success('变更已提交审批，审批通过后生效')
      return
    }
    ElMessage.error('保存失败: ' + errMsg(e, ''))
  }
}

/** 删除文件夹（仅 FOLDER 可操作；存在子文件夹或文档时无法删除） */
function deleteNode(id) {
  confirm({
    message: '注意：文件夹下存在子文件夹或文档时无法删除。',
    title: '删除文件夹', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    await api.deleteJson(NODE_API + '/' + id + '/')
    ElMessage.success('文件夹已删除')
    selectedNodeId.value = null
    selectedNode.value = null
    loadTree()
  })
}

/* ==========================================================
   文档列表（展示 / 分页 / 筛选 / 搜索 / 预览 / 分享 /
   申请权限 / 访问管理 / 编辑可见范围 / 删除）
   ========================================================== */
const docListVisible = ref(false)
const docListLoading = ref(false)
const docListDocs = ref([])
// 文档列表分页：翻页/改每页条数回调统一由 usePagination 管理（loadDocList 接收页码）
const { page: docListPage, pageSize: docListPageSize, onPageChange: onDocPageChange, onPageSizeChange, guardOverflow } = usePagination(loadDocList)
const docListTotal = ref(0)
const docListNodeFilter = ref(null)   // 当前节点筛选（null=全部）
const docSearch = ref('')
const docStatusFilter = ref('')
const docVisFilter = ref('')
const docTypeFilter = ref('')
const docShowAll = ref(false)
let docRequestSeq = 0         // 请求序号：防止快速连续操作时旧请求后返回覆盖新状态

/* ---- 文档列表表格高度：弹窗 body 只提供占位，表格必须拿到确定像素高度 ----
   el-table 的 height 传百分比时，在弹窗 flex 链下不会形成确定的内部高度
   （实测 inner-wrapper 仍按内容全高被裁剪，导致无法内部滚动、表头跟着内容走），
   因此这里用 ResizeObserver 测量"容器高度 - 筛选栏 - 分页"得到表格可用像素高度。
   弹窗 destroy-on-close，容器仅在打开时存在，故在 open 时测量并挂监听。 */
const docListBodyRef = ref(null)
const docTableHeight = ref(400)
let docListResizeObs = null

function measureDocTableHeight() {
  const wrap = docListBodyRef.value
  if (!wrap) return
  const fixed = (wrap.querySelector('.doc-filter-bar')?.offsetHeight || 0)
    + (wrap.querySelector('.doc-pagination')?.offsetHeight || 0)
  docTableHeight.value = Math.max(200, wrap.clientHeight - fixed)
}

/** 分页/筛选/搜索切换后表格回到顶部，避免停留在上一页的滚动位置。
    注意：el-table 设置 height 后正文由 el-scrollbar 包装，真正滚动的是
    .el-table__body-wrapper 内的 .el-scrollbar__wrap（body-wrapper 自身 overflow:hidden）。 */
function scrollDocTableToTop() {
  nextTick(() => {
    const root = docListBodyRef.value
    if (!root) return
    const wrap = root.querySelector('.el-table__body-wrapper .el-scrollbar__wrap')
      || root.querySelector('.el-table__body-wrapper')
    if (wrap) wrap.scrollTop = 0
  })
}

function onDocListOpened() {
  nextTick(() => {
    measureDocTableHeight()
    if (docListResizeObs) docListResizeObs.disconnect()
    if (docListBodyRef.value && typeof ResizeObserver !== 'undefined') {
      docListResizeObs = new ResizeObserver(() => measureDocTableHeight())
      docListResizeObs.observe(docListBodyRef.value)
    }
  })
}

function onDocListClosed() {
  docListResizeObs?.disconnect()
  docListResizeObs = null
}

// 数据加载后分页条出现/消失、行数变化都会影响布局，加载完成后再测一次表格高度
watch(docListDocs, () => nextTick(measureDocTableHeight))

/* ---- 搜索输入防抖（统一走 utils/debounce，300ms 内只触发一次加载） ---- */
const onDocSearchInput = debounce(() => loadDocList(1), 300)

/** 从节点详情页点击"查看本节点文档" */
function viewNodeDocs(nodeId) {
  setDocNodeFilter(nodeId)
  docListVisible.value = true
  loadDocList(1)
}

function setDocNodeFilter(nodeId) {
  docListNodeFilter.value = nodeId
}

/** 加载文档列表（分页/筛选/搜索，携带请求序号守卫） */
async function loadDocList(page) {
  const seq = ++docRequestSeq
  docListPage.value = page || 1
  docListLoading.value = true
  let url = DOC_API + '/?discover=1&page=' + docListPage.value + '&page_size=' + docListPageSize.value

  const search = (docSearch.value || '').trim()
  if (search) url += '&search=' + encodeURIComponent(search)
  if (docStatusFilter.value) url += '&status=' + encodeURIComponent(docStatusFilter.value)
  if (docVisFilter.value) url += '&visibility=' + encodeURIComponent(docVisFilter.value)
  if (docTypeFilter.value) url += '&file_type=' + encodeURIComponent(docTypeFilter.value)
  // 含旧版本：勾选后列表展示全部版本（含被新版本替换的非活跃版本），用于回溯与切换
  if (docShowAll.value) url += '&version=all'
  if (docListNodeFilter.value) url += '&node=' + docListNodeFilter.value
  // 默认只展示正式可用文档（已通过双审），与节点树 document_count 口径一致（避免"树 22 条、列表 64 条"的落差）；
  // 显式选择状态筛选时不追加——"已完成"后端已限定双审通过，其余状态需要看到处理中/失败的文档
  if (!docStatusFilter.value) url += '&audit_status=passed'

  try {
    const data = await api.getJson(url)
    if (seq !== docRequestSeq) return // 已有更新的请求发出，丢弃本次旧响应
    const docs = data.results || data || []
    docListDocs.value = docs
    docListTotal.value = data.count || docs.length || 0
    scrollDocTableToTop()
    // 数据量减少（文档被删除/恢复）导致当前页越界时，回退到最后一页重新加载
    if (guardOverflow(docListTotal.value)) return
  } catch (e) {
    if (seq !== docRequestSeq) return
    ElMessage.error('加载失败：' + errMsg(e, ''))
  } finally {
    if (seq === docRequestSeq) docListLoading.value = false
  }
}

// 切换每页条数：由 usePagination.onPageSizeChange 统一处理（重置回第 1 页并重新请求）

/** 文档状态标签：复用共享流水线合并状态（主解析 + 图谱/wiki 阶段） */
function statusTag(doc) {
  const [ptype, ptext] = pipelineStatus(doc || {})
  return { type: ptype === 'default' ? 'info' : ptype, text: ptext }
}

/** 从 nodeTree 查找文档的团队名称 */
function getDocTeamName(d) {
  if (!d || !d.team_node_id) return '—'
  const node = findNodeById(d.team_node_id)
  return node ? node.name : '—'
}

/* ==========================================================
   申请权限弹窗
   ========================================================== */
const reqVisible = ref(false)
const reqDoc = ref({})
const reqForm = reactive({ id: null, type: 'read', reason: '' })

function openReqModal(id) {
  // 填充文档归属信息卡片，让申请人在提交前确认申请对象
  const doc = docListDocs.value.find(d => d.id === id) || {}
  reqDoc.value = doc
  reqForm.id = id
  reqForm.type = 'read'
  reqForm.reason = ''
  reqVisible.value = true
}

async function submitRequest() {
  try {
    await api.postJson(DOC_API + '/' + reqForm.id + '/request_access/', {
      access_type: reqForm.type, reason: (reqForm.reason || '').trim()
    })
    reqVisible.value = false
    ElMessage.success('申请已提交，等待审批')
  } catch (e) {
    ElMessage.error(errMsg(e, '申请失败'))
  }
}

/* ==========================================================
   访问管理弹窗（已授权 + 待审批）
   ========================================================== */
const accessVisible = ref(false)
const accessDocId = ref(null)
const grantsLoading = ref(false)
const reqsLoading = ref(false)
const grants = reactive({ dept_grants: [], team_grants: [], direct_grants: [] })
const pendingReqs = ref([])

/** 合并部门/团队/直接授权为统一表格行 */
const grantRows = computed(() => {
  const rows = []
  ;(grants.dept_grants || []).forEach(d => {
    rows.push({ kind: 'dept', name: d.name, action: '读取', source: '部门权限', expires: '永久', active: true, revocable: false })
  })
  ;(grants.team_grants || []).forEach(t => {
    rows.push({ kind: 'team', name: t.name, action: '读取', source: '团队权限', expires: '永久', active: true, revocable: false })
  })
  ;(grants.direct_grants || []).forEach(g => {
    rows.push({
      kind: 'user',
      name: g.granted_to_name || '-',
      action: ACCESS_ACTION_MAP[g.action] || g.action,
      source: ACCESS_SOURCE_MAP[g.source] || g.source || '-',
      expires: g.expires_at ? formatDate(g.expires_at) : '永久',
      active: !!g.is_active,
      revocable: !!g.is_active,
      grantId: g.id,
      docId: accessDocId.value
    })
  })
  return rows
})

function openAccessModal(id) {
  accessDocId.value = id
  accessVisible.value = true
  loadDocGrants(id)
  loadDocRequests(id)
}

async function loadDocGrants(id) {
  grantsLoading.value = true
  try {
    const data = await api.getJson(DOC_API + '/' + id + '/access_grants/')
    if (!data) {
      grants.dept_grants = []
      grants.team_grants = []
      grants.direct_grants = []
      return
    }
    grants.dept_grants = data.dept_grants || []
    grants.team_grants = data.team_grants || []
    grants.direct_grants = data.direct_grants || []
  } catch (e) {
    ElMessage.error('加载失败')
  } finally {
    grantsLoading.value = false
  }
}

async function loadDocRequests(id) {
  reqsLoading.value = true
  try {
    // 拉取待审批申请（仅该文档）
    const reqs = await api.getJson(DOC_API + '/pending_access_requests/')
    pendingReqs.value = (reqs || []).filter(r => r.document_id == id && r.status === 'pending')
  } catch (e) {
    pendingReqs.value = []
    ElMessage.error('加载失败')
  } finally {
    reqsLoading.value = false
  }
}

function revokeGrant(row) {
  confirm({
    message: '确认撤销该用户的访问权限？',
    title: '撤销访问权限', confirmText: '确认撤销', errorText: '撤销失败',
  }, async () => {
    await api.postJson(DOC_API + '/' + row.docId + '/revoke_grant/', { grant_id: row.grantId })
    ElMessage.success('已撤销')
    loadDocGrants(row.docId)
  })
}

async function approveReq(reqId) {
  try {
    await api.postJson(DOC_API + '/approve_access_request/', { request_id: reqId })
    ElMessage.success('已批准')
    loadDocGrants(accessDocId.value)
    loadDocRequests(accessDocId.value)
  } catch (e) {
    ElMessage.error(errMsg(e, '操作失败'))
  }
}

function rejectReq(reqId) {
  confirm({
    message: '确认驳回该申请？',
    title: '驳回申请', confirmText: '确认驳回',
  }, async () => {
    await api.postJson(DOC_API + '/reject_access_request/', { request_id: reqId })
    ElMessage.success('已驳回')
    loadDocRequests(accessDocId.value)
  })
}

/* ==========================================================
   文档设置弹窗 — 可见范围调整
   ========================================================== */
const visVisible = ref(false)
const visDoc = ref(null)
const visOldScope = ref('team')
const visOptions = ref([])
const visSelectValue = ref('')
const visOwnership = ref('')
const visDeptList = ref([])
const visTeamList = ref([])
const showVisUpgradeHint = ref(false)
const showNarrowPanel = ref(false)
const showNarrowDeptSelect = ref(false)
const visNarrowHint = ref('')
const narrowDepts = ref([])
const narrowTeams = ref([])

async function loadDeptTeamOptions() {
  try {
    const res = await api.getJson(DOC_API + '/allowed_visibility/')
    visDeptList.value = res.departments || []
    visTeamList.value = res.teams || []
  } catch (e) {
    visDeptList.value = []
    visTeamList.value = []
  }
}

async function openVisModal(id) {
  const doc = docListDocs.value.find(d => d.id === id)
  if (!doc) { ElMessage.error('文档信息缺失'); return }
  visDoc.value = doc
  visOldScope.value = doc.visible_scope || 'team'
  visOwnership.value = buildDocOwnershipPath(doc)

  // 根据归属类型构建下拉选项：团队文档可向上扩到部门/公开；部门文档可缩小到指定团队；
  // 公开文档仅管理员可操作缩小（部门/团队）
  const oldScope = visOldScope.value
  let options = []
  if (oldScope === 'team') {
    options = [
      { value: 'team', text: '本团队（当前）' },
      { value: 'dept', text: '本部门' },
      { value: 'public', text: '全公司公开' }
    ]
  } else if (oldScope === 'dept') {
    options = [
      { value: 'narrow_teams', text: '指定团队（缩小范围）' },
      { value: 'dept', text: '本部门（当前）' },
      { value: 'public', text: '全公司公开' }
    ]
  } else {
    options = [
      { value: 'narrow_depts', text: '指定部门（缩小范围）' },
      { value: 'narrow_teams', text: '指定团队（缩小范围）' },
      { value: 'public', text: '全公司公开（当前）' }
    ]
  }
  visOptions.value = options
  visSelectValue.value = oldScope

  // 加载部门/团队列表并初始化缩小范围面板
  await loadDeptTeamOptions()
  onDocVisChange()
  visVisible.value = true
}

/** 构建文档归属路径显示 */
function buildDocOwnershipPath(doc) {
  const parts = []
  if (doc.dept_node_id) {
    const deptNode = findNodeById(doc.dept_node_id)
    if (deptNode) parts.push(deptNode.name)
  }
  if (doc.team_node_id) {
    const teamNode = findNodeById(doc.team_node_id)
    if (teamNode) parts.push(teamNode.name)
  }
  return parts.length > 0 ? parts.join(' / ') : '公司'
}

/** 可见范围变化：联动扩大提示与缩小范围面板（narrow_* 选项本身就是缩小） */
function onDocVisChange() {
  const vis = visSelectValue.value
  // 扩大范围提示
  const isUpgrade = (SCOPE_ORDER[vis] || 0) > (SCOPE_ORDER[visOldScope.value] || 0)
  const isNarrowOption = vis === 'narrow_teams' || vis === 'narrow_depts'
  showVisUpgradeHint.value = isUpgrade && !isNarrowOption

  if (isNarrowOption) {
    showNarrowPanel.value = true
    narrowDepts.value = []
    narrowTeams.value = []
    if (vis === 'narrow_teams' && visOldScope.value === 'dept') {
      // 部门文档 → 指定团队：仅显示本部门下的团队，无需再选部门
      showNarrowDeptSelect.value = false
      visNarrowHint.value = '选择可见的团队，未选择的团队将失去访问权限'
    } else if (vis === 'narrow_teams') {
      // 公开文档 → 指定团队：需要先选部门再选团队
      showNarrowDeptSelect.value = true
      visNarrowHint.value = '先选择部门，再选择该部门下的可见团队'
    } else if (vis === 'narrow_depts') {
      // 公开文档 → 指定部门
      showNarrowDeptSelect.value = true
      visNarrowHint.value = '选择可见的部门，未选择的部门将失去访问权限'
    }
  } else {
    showNarrowPanel.value = false
  }
}

/** 获取文档所属的部门模型 ID（从 dept_node_id 查找节点 ref_id） */
function getDocDeptId() {
  if (!visDoc.value || !visDoc.value.dept_node_id) return null
  const deptNode = findNodeById(visDoc.value.dept_node_id)
  return deptNode ? deptNode.ref_id : null
}

// 缩小范围面板中可选的团队（按场景过滤部门）：
// 部门文档→指定团队场景只展示本部门团队；其余场景按已选部门过滤
const narrowTeamOptions = computed(() => {
  if (visSelectValue.value === 'narrow_teams' && visOldScope.value === 'dept') {
    const deptId = getDocDeptId()
    if (!deptId) return []
    return visTeamList.value.filter(t => String(t.department_id || t.department) === String(deptId))
  }
  if (!narrowDepts.value.length) return []
  const deptIds = narrowDepts.value.map(String)
  return visTeamList.value.filter(t => deptIds.includes(String(t.department_id || t.department)))
})

// 团队选择是否可用：部门文档→指定团队场景直接可用，其余需先选部门
const narrowDeptSelected = computed(() => {
  if (visSelectValue.value === 'narrow_teams' && visOldScope.value === 'dept') return true
  return narrowDepts.value.length > 0
})

const narrowTeamPlaceholder = computed(() => {
  return narrowDeptSelected.value ? '请选择团队' : '请先选择部门'
})

// 部门选择变化：清空不属于所选部门的团队选择
// （旧版 multi-select 重新渲染面板后，非匹配部门的团队勾选同样会丢失）
function onNarrowDeptChange() {
  const deptIds = narrowDepts.value.map(String)
  narrowTeams.value = narrowTeams.value.filter(tid => {
    const team = visTeamList.value.find(x => x.id === tid)
    return team && deptIds.includes(String(team.department_id || team.department))
  })
}

/** 保存可见范围调整；缩小范围时附带创建跨团队授权 */
async function saveDocVis() {
  const id = visDoc.value.id
  const vis = visSelectValue.value

  // narrowing 选项映射
  const isNarrow = vis === 'narrow_teams' || vis === 'narrow_depts'
  let actualScope = vis
  let selTeams = []
  let selDepts = []

  if (vis === 'narrow_teams') {
    actualScope = 'team'
    selTeams = narrowTeams.value.slice()
    if (!selTeams.length) { ElMessage.warning('请至少选择一个团队'); return }
  } else if (vis === 'narrow_depts') {
    actualScope = 'dept'
    selDepts = narrowDepts.value.slice()
    // 同时收集选中的部门下的团队
    selTeams = narrowTeams.value.slice()
    if (!selDepts.length && !selTeams.length) { ElMessage.warning('请至少选择一个部门或团队'); return }
  }

  // 无变化
  if (!isNarrow && vis === visOldScope.value) { visVisible.value = false; return }

  // 向上调整 / 向下调整
  const isUpgrade = !isNarrow && (SCOPE_ORDER[actualScope] || 0) > (SCOPE_ORDER[visOldScope.value] || 0)

  try {
    await api.patchJson(DOC_API + '/' + id + '/', { visible_scope: actualScope })
    // 缩小范围：创建跨团队授权
    if (isNarrow && selTeams.length > 0) {
      await createNarrowGrants(id, selTeams)
    }
    visVisible.value = false
    ElMessage.success(isNarrow ? '可见范围已缩小' : (isUpgrade ? '设置已保存' : '可见范围已缩小'))
    loadDocList(docListPage.value)
  } catch (e) {
    if (isUpgrade) {
      // 扩大可见范围被工单审批拦截：后端已自动提交双层审批申请，按成功提示告知用户
      visVisible.value = false
      ElMessage.warning('可见范围扩大需双层审批，已自动提交申请，需两位管理员先后审批')
      return
    }
    ElMessage.error(errMsg(e, '保存失败'))
  }
}

/** 缩小范围时，创建跨团队授权（可能因已存在而失败，忽略） */
async function createNarrowGrants(docId, teamIds) {
  const tasks = teamIds.map(teamId => {
    // 从 visTeamList 中查找 team_code
    const team = visTeamList.value.find(t => t.id === teamId)
    const teamCode = team ? (team.code || '') : ''
    if (!teamCode) return Promise.resolve()
    return api.postJson(DOC_API + '/' + docId + '/grant_access/', {
      grant_type: 'cross_team',
      team_code: teamCode
    }).catch(() => { /* 跨团队授权可能因已存在而失败，忽略 */ })
  })
  await Promise.all(tasks)
}

/* ==========================================================
   删除文档
   ========================================================== */
function deleteDoc(id) {
  confirm({
    message: '确认删除此文档？删除后不可恢复。',
    title: '删除文档', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    await api.deleteJson(DOC_API + '/' + id + '/')
    ElMessage.success('文档已删除')
    loadDocList(docListPage.value)
  })
}

/* ==========================================================
   文档预览（公共组件 DocPreviewDialog：显隐与打开参数）
   ========================================================== */
const previewVisible = ref(false)
const previewDocId = ref(null)        // 当前预览文档 ID
const previewInitialPage = ref(1)     // 打开预览定位页（image 为页号）

// 打开文档预览弹窗（默认第 1 页）
function openPreview(id) {
  if (!id) return
  previewDocId.value = id
  previewInitialPage.value = 1
  previewVisible.value = true
}

/* ==========================================================
   版本历史弹窗（同组版本列表 + 设为活跃）
   ========================================================== */
const versionVisible = ref(false)
const versionLoading = ref(false)
const versionTitle = ref('版本历史')
const versionList = ref([])
const versionDocId = ref(null)

async function showVersionModal(docId) {
  versionDocId.value = docId
  versionVisible.value = true
  versionLoading.value = true
  versionList.value = []
  versionTitle.value = '版本历史'
  try {
    const data = await api.getJson(DOC_API + '/' + docId + '/versions/')
    const docs = data.documents || []
    versionList.value = docs
    // 弹窗标题取活跃版本标题（无活跃时取最新一条），避免在模板中直接嵌入用户输入
    const titleDoc = docs.find(v => v.is_active) || docs[0]
    if (titleDoc && titleDoc.title) versionTitle.value = '版本历史 · ' + titleDoc.title
  } catch (e) {
    ElMessage.error('加载失败：' + errMsg(e, ''))
  } finally {
    versionLoading.value = false
  }
}

// 设为活跃版本：切换成功后刷新弹窗内版本列表 + 文档列表（与旧版行为一致）
async function setVersionActive(versionId, docId) {
  try {
    await api.postJson(DOC_API + '/' + versionId + '/set_active/')
    ElMessage.success('已切换为活跃版本')
    showVersionModal(docId)
    loadDocList(docListPage.value)
  } catch (e) {
    ElMessage.error(errMsg(e, '切换失败'))
  }
}

/* ==========================================================
   页面初始化 / 清理
   ========================================================== */
onMounted(() => {
  userStore.restore()
  // 非管理员/团队组长不显示新增按钮（canManageNodes() 控制）
  loadRootTypes().then(() => {
    loadTree()
  })
})

onBeforeUnmount(() => {
  onDocSearchInput.cancel()  // 卸载时取消挂起的防抖搜索
})
</script>

<style scoped>
/* ===== 主体：左树右详情（page-body 内撑满，两侧面板各自管理滚动） ===== */
.node-manage {
  display: flex;
  gap: 16px;
  flex: 1;
  min-height: 0;
}

.node-tree-panel {
  width: 340px;
  flex-shrink: 0;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.panel-body {
  flex: 1;
  overflow-y: auto;
  /* 四边等距；滚动条宽度常驻 6px（见下方 ::-webkit-scrollbar），
     悬停出现滚动条时不会改变内容宽度 */
  padding: 12px;
}

/* Firefox 兜底：默认透明隐藏，悬停（滑动）时显示。
   -moz-appearance 仅 Firefox 支持：避免标准 scrollbar-width 在 Chromium 也预留轨道空间 */
@supports (-moz-appearance: none) {
  .panel-body {
    scrollbar-width: thin;
    scrollbar-color: transparent transparent;
  }

  .panel-body:hover {
    scrollbar-color: var(--app-border) transparent;
  }
}

/* 节点树细滚动条（6px 细条）：宽度常驻，因此"平时隐藏 / 悬停出现"只切换 thumb 颜色，
   不改变布局宽度，避免滚动条出现时面板内容抖动 */
.panel-body::-webkit-scrollbar {
  width: 6px;
  height: 6px;
}

.panel-body::-webkit-scrollbar-track {
  background: transparent;
}

/* 平时 thumb 透明（滚动条不可见）；悬停/滚动时显示，避免系统粗滚动条 */
.panel-body::-webkit-scrollbar-thumb {
  background: transparent;
  border-radius: 3px;
}

.panel-body:hover::-webkit-scrollbar-thumb {
  background: var(--app-border);
}

.panel-body:hover::-webkit-scrollbar-thumb:hover {
  background: var(--app-text-sub);
}

/* el-tree 节点内容：图标 + 名称 + 文档计数 */
.tree-node-item {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  font-size: 13px;
}

.tree-node-icon {
  width: 16px;
  text-align: center;
  flex-shrink: 0;
}

.tree-node-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tree-node-count {
  font-size: 11px;
  color: var(--app-text-sub);
  margin-left: 6px;
  /* 与右边缘保持间距，避免数字贴边 */
  margin-right: 10px;
  flex-shrink: 0;
}

.node-detail-panel {
  flex: 1;
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 24px;
  overflow-y: auto;
}

/* ===== 节点详情 ===== */
.nd-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
}

.nd-head-left {
  min-width: 0;
}

.nd-title {
  font-size: 20px;
  font-weight: 600;
  color: var(--app-text);
  display: flex;
  align-items: center;
  gap: 8px;
}

.nd-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nd-meta {
  margin-top: 8px;
}

.nd-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  align-items: center;
}

/* 编辑 / 删除文件夹按钮高度统一，避免因文字/图标排版导致两侧按钮高度不一致 */
.nd-actions .el-button {
  height: 24px;
}

.nd-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}

.stat-card {
  background: var(--app-card-bg);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 16px;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.stat-card:hover {
  border-color: #409eff;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

.nd-doc-card:hover {
  cursor: pointer;
}

.stat-card-label {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-bottom: 4px;
}

.stat-card-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.stat-card-value {
  font-size: 22px;
  font-weight: 600;
  color: var(--app-text);
}

.stat-card-link {
  font-size: 11px;
  color: #409eff;
}

.nd-info-card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 16px;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  margin-bottom: 12px;
}

.nd-info-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px 24px;
}

.nd-info-value {
  font-weight: 500;
  color: var(--app-text);
  margin-top: 4px;
  font-size: 13px;
}

.nd-desc {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--app-border);
}

.nd-desc-title {
  margin-bottom: 8px;
}

.nd-desc-text {
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  color: var(--app-text);
}

/* ===== 文档列表弹窗布局 =====
   BaseDialog 只提供 body 占位，这里由页面控制内部格式：
   筛选栏/分页固定高度；表格高度由页面用 ResizeObserver 测量成确定像素值
   （:height="docTableHeight"），从而保证"表头固定 + 正文内部滚动"。
   注意：el-dialog 通过 teleport 渲染到 body，弹窗外壳类需用 :global() 包裹 */
.doc-list-body {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

:global(.base-dialog) .doc-filter-bar {
  flex-shrink: 0;
}

/* 表格：高度已是确定的像素值（:height），只禁止被 flex 压缩，不再撑满剩余空间 */
:global(.base-dialog .el-table) {
  flex-shrink: 0;
}

:global(.base-dialog) .doc-pagination {
  flex-shrink: 0;
}

/* 访问管理弹窗 body 布局：整体内部滚动（页面控制，BaseDialog 只提供占位） */
.access-body {
  height: 100%;
  overflow: auto;
}

.doc-count-sub {
  margin-left: 8px;
  font-weight: 400;
}

.doc-filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 0 0 14px;
}

.doc-file-name {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-pagination {
  margin-top: 16px;
  justify-content: flex-end;
}

/* ===== 弹窗通用表单 ===== */
.form-section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 8px 0 12px;
  color: var(--app-text);
}

.form-item.mb-16 {
  margin-bottom: 16px;
}

.form-item.mb-12 {
  margin-bottom: 12px;
}

.form-row {
  display: flex;
  gap: 16px;
}

.flex-1 {
  flex: 1;
}

.order-input {
  flex-shrink: 0;
}

.node-basic-hint {
  margin-top: -8px;
  margin-bottom: 14px;
}

/* ===== 文档归属信息卡片 ===== */
.doc-ownership-card {
  background: var(--app-bg);
  border: 1px solid var(--app-border);
  border-radius: 6px;
  padding: 16px;
  margin-bottom: 20px;
}

.doc-ownership-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--app-border);
}

.doc-ownership-icon {
  font-size: 20px;
  flex-shrink: 0;
}

.doc-ownership-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.doc-ownership-meta {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px 16px;
}

.doc-ownership-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.doc-ownership-label {
  color: var(--app-text-sub);
  flex-shrink: 0;
}

.doc-ownership-value {
  color: var(--app-text);
}

/* ===== 提示样式 ===== */
.hint-warning {
  background: #fff8e1;
  border: 1px solid #ffc107;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 13px;
  color: #8d6e00;
  margin: 12px 0;
}

/* ===== 缩小范围面板 ===== */
.narrow-panel {
  margin-top: 12px;
}

.multi-select-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

</style>
