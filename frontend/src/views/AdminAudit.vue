<template>
  <div class="page-container admin-audit-page">
    <!-- ===== 页头 ===== -->
    <div class="page-header">
      <div class="audit-page-head">
        <div class="page-title">审计日志与安全</div>
        <div class="page-desc">全链路操作留痕，IP 黑白名单管控，登录异常追踪</div>
      </div>
    </div>

    <!-- ===== 内容卡片：tabs 撑满高度，面板内部滚动 ===== -->
    <div class="page-body">
    <div class="app-card audit-card tabs-fill">
      <el-tabs v-model="activeTab" @tab-change="onTabChange">
        <!-- ===== 📋 审计日志 ===== -->
        <el-tab-pane label="📋 审计日志" name="audit">
          <div class="filter-bar audit-filter-bar">
            <el-input v-model="auditFilter.username" placeholder="🔍 用户名" clearable style="width: 180px" @keyup.enter="loadAuditLogs" @clear="loadAuditLogs" />
            <el-select v-model="auditFilter.action" placeholder="全部操作类型" clearable style="width: 150px" @change="loadAuditLogs">
              <el-option v-for="opt in AUDIT_ACTION_FILTERS" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
            <el-input v-model="auditFilter.ip" placeholder="🌐 IP 地址" clearable style="width: 200px" @keyup.enter="loadAuditLogs" @clear="loadAuditLogs" />
            <el-date-picker v-model="auditFilter.startDate" type="date" placeholder="开始日期" value-format="YYYY-MM-DD" style="width: 150px" />
            <span class="text-sub">至</span>
            <el-date-picker v-model="auditFilter.endDate" type="date" placeholder="结束日期" value-format="YYYY-MM-DD" style="width: 150px" />
            <el-button type="primary" size="small" style="width: 72px" @click="loadAuditLogs">查询</el-button>
            <el-button size="small" style="width: 72px" @click="resetAuditFilter">重置</el-button>
            <div class="filter-spacer"></div>
            <el-button type="primary" size="small" @click="exportAuditLogs">📥 导出 Excel</el-button>
          </div>
          <div class="tab-table-wrap">
          <el-table :data="auditRows" v-loading="auditLoading" class="audit-table" height="100%">
            <el-table-column label="时间" width="150">
              <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="用户" min-width="100">
              <template #default="{ row }"><span class="fw-500">{{ row.actor_username || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="操作类型" width="110">
              <template #default="{ row }">
                <el-tag :type="opTagType(row.action)" size="small" effect="plain">{{ opLabel(row.action) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="资源" min-width="120">
              <template #default="{ row }">{{ formatResource(row.target_type, row.target_id) }}</template>
            </el-table-column>
            <el-table-column label="IP 地址" width="130">
              <template #default="{ row }"><code class="ip-code">{{ row.ip_address || '-' }}</code></template>
            </el-table-column>
            <el-table-column label="结果" width="100">
              <template #default="{ row }">
                <el-tag :type="resultTagType(row.result)" size="small" effect="plain">{{ resultLabel(row.result) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="详情" width="80">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="showAuditDetail(row)">展开 ›</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无审计日志" :image-size="60" /></template>
          </el-table>
          </div>
          <AppPagination
            class="tab-pagination"
            :total="auditTotal"
            :page-size="TAB_PAGE_SIZE"
            :page="auditPage"
            @page-change="onAuditPageChange"
          />
        </el-tab-pane>

        <!-- ===== ✅ IP 白名单 ===== -->
        <el-tab-pane label="✅ IP 白名单" name="white">
          <div class="tab-head">
            <div class="tab-info-bar">
              <span class="tab-info-icon">✅</span>
              <span>命中白名单的 IP 直接放行（即使同 IP 也在黑名单中）；白名单外的 IP 会继续检查黑名单；都不命中则默认放行，共 <strong class="tab-info-count">{{ whitelistCache.length }}</strong> 条规则</span>
            </div>
            <el-button type="primary" size="small" @click="openWhitelistDialog()">＋ 新增白名单</el-button>
          </div>
          <div class="tab-table-wrap">
          <el-table :data="whitelistCache" v-loading="whiteLoading" class="audit-table" height="100%">
            <el-table-column label="IP / CIDR" min-width="160">
              <template #default="{ row }"><code class="ip-code">{{ row.ip_or_cidr }}</code></template>
            </el-table-column>
            <el-table-column label="说明" min-width="140" prop="description" show-overflow-tooltip>
              <template #default="{ row }">{{ row.description || '-' }}</template>
            </el-table-column>
            <el-table-column label="添加人" width="120" prop="creator" show-overflow-tooltip>
              <template #default="{ row }">{{ row.creator || '-' }}</template>
            </el-table-column>
            <el-table-column label="添加时间" width="150">
              <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openWhitelistDialog(row)">编辑</el-button>
                <el-button link type="danger" size="small" @click="deleteWhitelist(row.id)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无白名单" :image-size="60" /></template>
          </el-table>
          </div>
        </el-tab-pane>

        <!-- ===== 🚫 IP 黑名单 ===== -->
        <el-tab-pane label="🚫 IP 黑名单" name="black">
          <div class="tab-head">
            <div class="tab-info-bar">
              <span class="tab-info-icon">🚫</span>
              <span>黑名单 IP 将被<strong class="danger-text">永久拒绝</strong>访问（但若同 IP 也在白名单中则优先放行），登录失败 5 次自动封禁 15 分钟</span>
            </div>
            <el-button type="danger" size="small" @click="openBlacklistDialog">＋ 手动封禁</el-button>
          </div>
          <div class="tab-table-wrap">
          <el-table :data="blackPageItems" v-loading="blackLoading" class="audit-table" height="100%">
            <el-table-column label="IP 地址" min-width="140">
              <template #default="{ row }"><code class="ip-code">{{ row.ip }}</code></template>
            </el-table-column>
            <el-table-column label="封禁原因" width="130">
              <template #default="{ row }">{{ blacklistReason(row.reason) }}</template>
            </el-table-column>
            <el-table-column label="操作人" width="120" prop="detail" show-overflow-tooltip>
              <template #default="{ row }">{{ row.detail || '系统自动' }}</template>
            </el-table-column>
            <el-table-column label="封禁时间" width="150">
              <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="解封时间" width="140">
              <template #default="{ row }">
                <span v-if="row.expires_at" class="text-sub">{{ formatDate(row.expires_at) }}</span>
                <el-tag v-else type="danger" size="small">永久</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="unblockIp(row.id)">解封</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无黑名单" :image-size="60" /></template>
          </el-table>
          </div>
          <AppPagination
            class="tab-pagination"
            :total="blacklistCache.length"
            :page-size="TAB_PAGE_SIZE"
            :page="blackPage"
            @page-change="blackPage = $event"
          />
        </el-tab-pane>

        <!-- ===== 🛡️ 敏感词 ===== -->
        <el-tab-pane label="🛡️ 敏感词" name="sensitive">
          <div class="tab-head">
            <div class="tab-info-bar">
              <span class="tab-info-icon">🛡️</span>
              <span>LLM 输出实时审查词库 — 命中 <el-tag type="danger" size="small" effect="plain">block</el-tag> 立即中断流式，<el-tag type="warning" size="small" effect="plain">mask</el-tag> 脱敏后下发，<el-tag type="info" size="small" effect="plain">warn</el-tag> 仅记录告警；命中次数为累计命中统计</span>
            </div>
            <el-button type="primary" size="small" @click="openSensitiveDialog()">＋ 新增敏感词</el-button>
          </div>
          <div class="tab-table-wrap">
          <el-table :data="sensitiveCache" v-loading="sensitiveLoading" class="audit-table" height="100%">
            <el-table-column label="敏感词" min-width="140">
              <template #default="{ row }"><code class="ip-code">{{ row.word }}</code></template>
            </el-table-column>
            <el-table-column label="分类" width="100">
              <template #default="{ row }">
                <el-tag :type="sensitiveCategoryType(row.category)" size="small" effect="plain">{{ sensitiveCategoryLabel(row.category) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="动作" width="90">
              <template #default="{ row }">
                <el-tag :type="sensitiveActionType(row.action)" size="small" effect="plain">{{ sensitiveActionLabel(row.action) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="命中次数" width="90">
              <template #default="{ row }">{{ row.hit_count || 0 }}</template>
            </el-table-column>
            <el-table-column label="正则" width="70">
              <template #default="{ row }">
                <el-tag v-if="row.is_regex" type="primary" size="small" effect="plain">是</el-tag>
                <el-tag v-else type="info" size="small" effect="plain">否</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="70">
              <template #default="{ row }">
                <el-tag v-if="row.is_enabled" type="success" size="small" effect="plain">启用</el-tag>
                <el-tag v-else type="info" size="small" effect="plain">禁用</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="添加时间" width="150">
              <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="120">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="openSensitiveDialog(row)">编辑</el-button>
                <el-button link type="danger" size="small" @click="deleteSensitive(row.id)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无敏感词，点击右上角新增" :image-size="60" /></template>
          </el-table>
          </div>
        </el-tab-pane>

        <!-- ===== 🔐 登录尝试 ===== -->
        <el-tab-pane label="🔐 登录尝试" name="login">
          <div class="tab-info-bar login-info">
            <span class="tab-info-icon">🔐</span>
            <span>近 24 小时登录尝试记录，异常尝试将<strong class="danger-text">自动加入黑名单</strong></span>
          </div>
          <div class="filter-bar audit-filter-bar">
            <el-input v-model="loginFilter.username" placeholder="🔍 用户名" clearable style="width: 160px" @keyup.enter="loadLoginAttempts" @clear="loadLoginAttempts" />
            <el-input v-model="loginFilter.ip" placeholder="🌐 IP 地址" clearable style="width: 180px" @keyup.enter="loadLoginAttempts" @clear="loadLoginAttempts" />
            <el-select v-model="loginFilter.result" placeholder="全部结果" clearable style="width: 130px" @change="loadLoginAttempts">
              <el-option label="成功" value="success" />
              <el-option label="失败" value="wrong_password" />
            </el-select>
            <el-button type="primary" size="small" style="width: 72px" @click="loadLoginAttempts">查询</el-button>
            <el-button size="small" style="width: 72px" @click="resetLoginFilter">重置</el-button>
            <div class="filter-spacer"></div>
            <el-button type="primary" size="small" @click="exportLoginAttempts">📥 导出 Excel</el-button>
          </div>
          <div class="tab-table-wrap">
          <el-table :data="loginRows" v-loading="loginLoading" class="audit-table" height="100%">
            <el-table-column label="时间" width="150">
              <template #default="{ row }"><span class="text-sub">{{ formatDate(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="用户" min-width="100">
              <template #default="{ row }"><span class="fw-500">{{ row.username || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="IP 地址" width="130">
              <template #default="{ row }"><code class="ip-code">{{ row.ip }}</code></template>
            </el-table-column>
            <el-table-column label="User-Agent" min-width="180" show-overflow-tooltip>
              <template #default="{ row }"><span class="text-sub text-xs">{{ row.user_agent || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.result === 'success'" type="success" size="small" effect="plain">✓ 成功</el-tag>
                <el-tag v-else type="danger" size="small" effect="plain">✕ 失败</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="失败原因" width="110">
              <template #default="{ row }"><span class="text-sub">{{ loginFailReason(row.result) }}</span></template>
            </el-table-column>
            <template #empty><el-empty description="暂无登录记录" :image-size="60" /></template>
          </el-table>
          </div>
          <AppPagination
            class="tab-pagination"
            :total="loginTotal"
            :page-size="TAB_PAGE_SIZE"
            :page="loginPage"
            @page-change="onLoginPageChange"
          />
        </el-tab-pane>
      </el-tabs>
    </div>
    </div>

    <!-- ===== 审计详情弹窗 ===== -->
    <el-dialog v-model="auditDetailVisible" :title="`审计详情 · ${auditDetailRow ? (auditDetailRow.action || '审计记录') : ''}`" width="640px" top="6vh" :close-on-click-modal="false">
      <div v-if="auditDetailRow" class="audit-detail">
        <div class="detail-grid">
          <div class="detail-item">
            <div class="detail-label">时间</div>
            <div class="detail-value">{{ formatDate(auditDetailRow.created_at) }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">用户</div>
            <div class="detail-value">{{ auditDetailRow.actor_username || '-' }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">操作</div>
            <div class="detail-value">{{ auditDetailRow.action || '-' }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">资源</div>
            <div class="detail-value">{{ formatResource(auditDetailRow.target_type, auditDetailRow.target_id) }}</div>
          </div>
          <div class="detail-item">
            <div class="detail-label">IP 地址</div>
            <div class="detail-value"><code class="ip-code">{{ auditDetailRow.ip_address || '-' }}</code></div>
          </div>
          <div class="detail-item">
            <div class="detail-label">结果</div>
            <div class="detail-value">{{ auditDetailRow.result || '-' }}</div>
          </div>
        </div>
        <div class="detail-block">
          <div class="detail-label">上下文 JSON</div>
          <pre class="detail-pre">{{ auditDetailJson }}</pre>
        </div>
      </div>
      <template #footer>
        <el-button @click="auditDetailVisible = false">关闭</el-button>
        <el-button type="primary" @click="copyAuditJson">复制 JSON</el-button>
      </template>
    </el-dialog>

    <!-- ===== 白名单新增/编辑弹窗 ===== -->
    <el-dialog v-model="whitelistDialogVisible" :title="whitelistEditId ? '编辑白名单' : '新增白名单'" width="480px" :close-on-click-modal="false">
      <div class="tab-info-bar dlg-banner">
        <span class="tab-info-icon">{{ whitelistEditId ? '✏️' : '✅' }}</span>
        <span>{{ whitelistEditId ? '修改后立即生效' : '命中白名单的 IP 直接放行，白名单外的 IP 会继续检查黑名单' }}</span>
      </div>
      <el-form ref="whitelistFormRef" :model="whitelistForm" label-position="top">
        <el-form-item label="IP / CIDR" prop="ip_or_cidr" :rules="whitelistIpRules">
          <el-input v-model="whitelistForm.ip_or_cidr" placeholder="单 IP / CIDR / 通配符 / 范围，如 10.0.0.1、10.0.0.0/24、10.0.*.*" />
          <div class="form-hint">支持格式：单 IP（10.0.0.1）、CIDR（10.0.0.0/24）、通配符（10.0.*.*）、范围（10.0.0.1-10.0.0.100）</div>
        </el-form-item>
        <el-form-item label="说明" prop="description" :rules="[{ required: true, message: '请输入说明', trigger: 'blur' }]">
          <el-input v-model="whitelistForm.description" placeholder="原因, 便于后续审计追溯" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="whitelistDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="whitelistSaving" @click="saveWhitelist">保存</el-button>
      </template>
    </el-dialog>

    <!-- ===== 黑名单手动封禁弹窗 ===== -->
    <el-dialog v-model="blacklistDialogVisible" title="手动封禁 IP" width="480px" :close-on-click-modal="false">
      <div class="tab-info-bar dlg-banner danger-banner">
        <span class="tab-info-icon">🚫</span>
        <span>黑名单新增将立即生效，无需审批</span>
      </div>
      <el-form ref="blacklistFormRef" :model="blacklistForm" label-position="top">
        <el-form-item label="IP 地址" prop="ip" :rules="blacklistIpRules">
          <el-input v-model="blacklistForm.ip" placeholder="单 IP / 通配符 / 范围，如 10.0.0.1、10.0.*.*、10.0.0.1-10.0.0.100" />
          <div class="form-hint">支持格式：单 IP（10.0.0.1）、通配符（10.0.*.*）、范围（10.0.0.1-10.0.0.100）</div>
        </el-form-item>
        <el-form-item label="封禁原因" prop="reason" :rules="[{ required: true, message: '请输入封禁原因', trigger: 'blur' }]">
          <el-input v-model="blacklistForm.reason" placeholder="必填，便于后续审计追溯" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="blacklistDialogVisible = false">取消</el-button>
        <el-button type="danger" :loading="blacklistSaving" @click="saveBlacklist">确认封禁</el-button>
      </template>
    </el-dialog>

    <!-- ===== 敏感词新增/编辑弹窗 =====
         新增：word + category + action + is_regex 可编辑
         编辑：仅 action + is_enabled 可编辑（后端 PUT 限制），word/category 显示但禁用 -->
    <el-dialog v-model="sensitiveDialogVisible" :title="sensitiveEditId ? '编辑敏感词' : '新增敏感词'" width="520px" :close-on-click-modal="false">
      <div class="sensitive-action-guide">
        <div class="sensitive-action-item sensitive-action-block">
          <span class="sensitive-action-badge">block</span><span>命中立即中断流式输出</span>
        </div>
        <div class="sensitive-action-item sensitive-action-mask">
          <span class="sensitive-action-badge">mask</span><span>替换为 *** 后下发</span>
        </div>
        <div class="sensitive-action-item sensitive-action-warn">
          <span class="sensitive-action-badge">warn</span><span>仅记录告警日志</span>
        </div>
      </div>
      <el-form label-position="top">
        <el-form-item label="敏感词" required>
          <el-input v-model="sensitiveForm.word" :disabled="!!sensitiveEditId" maxlength="128" placeholder="输入敏感词（最长 128 字符）" />
        </el-form-item>
        <div class="form-row">
          <el-form-item label="分类" style="flex: 1">
            <el-select v-model="sensitiveForm.category" :disabled="!!sensitiveEditId" style="width: 100%">
              <el-option v-for="opt in SENSITIVE_CATEGORY_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="处理动作" style="flex: 1">
            <el-select v-model="sensitiveForm.action" style="width: 100%">
              <el-option v-for="opt in SENSITIVE_ACTION_OPTIONS" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="选项">
          <el-checkbox v-model="sensitiveForm.is_regex" :disabled="!!sensitiveEditId">正则表达式</el-checkbox>
          <!-- 新增时强制启用：后端 POST 硬编码 is_enabled=True，不接受该字段，
               禁用复选框避免用户误以为取消勾选可以创建即禁用 -->
          <el-checkbox v-model="sensitiveForm.is_enabled" :disabled="!sensitiveEditId" style="margin-left: 16px">启用</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sensitiveDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="sensitiveSaving" @click="saveSensitive">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '../api/http'
import { formatDate, validateIpPattern, errMsg } from '../utils/format'
import { makeStatusMeta } from '../utils/labels'
import { exportCsv } from '../utils/download'
import { usePagination } from '../composables/usePagination'
import { useConfirm } from '../composables/useConfirm'
import AppPagination from '../components/base/AppPagination.vue'

// 二次确认弹窗统一封装
const { confirm } = useConfirm()

/* ============ 常量与映射 ============ */
const TAB_PAGE_SIZE = 20 // 各 tab 统一每页条数

// 审计日志操作类型 → el-tag type / 中文文案（未知 op 兜底原值，防 XSS）
const OP_TAG_MAP = {
  login: 'info', upload_document: 'primary', delete_document: 'danger', update_user: 'warning',
  toggle_user_status: 'warning', export: 'success', create_node: 'default', chat_ask: 'default',
  manage_whitelist: 'default', manage_blacklist: 'danger', manage_sensitive_word: 'warning',
  logout: 'info', reset_password: 'warning', feedback: 'default', admin_users: 'warning',
  update_node: 'default', token_refresh: 'info',
}
const OP_LABEL_MAP = {
  login: '登录', upload_document: '上传', delete_document: '删除', update_user: '用户变更',
  toggle_user_status: '启禁用', export: '导出', create_node: '知识库', chat_ask: '问答',
  manage_whitelist: '白名单', manage_blacklist: '黑名单', manage_sensitive_word: '敏感词',
  logout: '登出', reset_password: '改密', feedback: '反馈', admin_users: '用户管理',
  update_node: '节点变更', token_refresh: '令牌刷新',
}
// 审计日志结果 → el-tag type / 文案（未知结果按 failed 处理）
const RESULT_MAP = {
  success: { type: 'success', label: '✓ 成功' },
  failed: { type: 'danger', label: '✕ 失败' },
  denied: { type: 'warning', label: '⚠ 拒绝' },
}

// 审计日志筛选栏的操作类型选项（与旧 HTML 下拉一致）
const AUDIT_ACTION_FILTERS = [
  { value: 'login', label: '登录' },
  { value: 'upload_document', label: '上传' },
  { value: 'delete_document', label: '删除' },
  { value: 'update_user', label: '用户变更' },
  { value: 'toggle_user_status', label: '启禁用' },
  { value: 'export', label: '导出' },
  { value: 'create_node', label: '知识库' },
  { value: 'chat_ask', label: '问答' },
  { value: 'manage_whitelist', label: '白名单' },
  { value: 'manage_blacklist', label: '黑名单' },
  { value: 'manage_sensitive_word', label: '敏感词' },
]

// 敏感词分类 → el-tag type / 文案（仅用于展示，不影响处理逻辑）
const SENSITIVE_CATEGORY_MAP = {
  phone: { label: '手机号', type: 'info' },
  id_card: { label: '身份证', type: 'warning' },
  email: { label: '邮箱', type: 'info' },
  bank_card: { label: '银行卡', type: 'warning' },
  secret: { label: '内部机密', type: 'danger' },
  other: { label: '其它', type: 'info' },
}
// 敏感词动作 → el-tag type / 文案（block=红 / mask=黄 / warn=灰，颜色与拦截卡片视觉一致）
const SENSITIVE_ACTION_MAP = {
  mask: { label: '脱敏', type: 'warning' },
  block: { label: '拦截', type: 'danger' },
  warn: { label: '告警', type: 'info' },
}
const SENSITIVE_CATEGORY_OPTIONS = [
  { value: 'other', label: '其它' },
  { value: 'phone', label: '手机号' },
  { value: 'id_card', label: '身份证' },
  { value: 'email', label: '邮箱' },
  { value: 'bank_card', label: '银行卡' },
  { value: 'secret', label: '内部机密' },
]
const SENSITIVE_ACTION_OPTIONS = [
  { value: 'mask', label: 'mask · 脱敏替换' },
  { value: 'block', label: 'block · 拦截中断' },
  { value: 'warn', label: 'warn · 仅告警' },
]

/* ============ 页面状态 ============ */
const activeTab = ref('audit') // 当前 tab（对应旧 STATE.currentAuditTab）

// 审计日志筛选条件
const auditFilter = reactive({ username: '', action: '', ip: '', startDate: '', endDate: '' })
const auditRows = ref([])
// 审计日志分页：翻页回调统一由 usePagination 管理（loadAuditPage 接收页码）
const { page: auditPage, onPageChange: onAuditPageChange } = usePagination(p => loadAuditPage(p))
const auditTotal = ref(0)
const auditLoading = ref(false)
let auditReqSeq = 0 // 翻页请求序号守卫：快速连续翻页时丢弃旧响应

// IP 白名单（全量展示，无分页）
const whitelistCache = ref([])
const whiteLoading = ref(false)

// IP 黑名单（前端分页）
const blacklistCache = ref([])
const blackPage = ref(1)
const blackLoading = ref(false)

// 敏感词（全量展示，无分页）
const sensitiveCache = ref([])
const sensitiveLoading = ref(false)

// 登录尝试（服务端分页）
const loginFilter = reactive({ username: '', ip: '', result: '' })
const loginRows = ref([])
// 登录记录分页：翻页回调统一由 usePagination 管理（loadLoginPage 接收页码）
const { page: loginPage, onPageChange: onLoginPageChange, guardOverflow } = usePagination(p => loadLoginPage(p))
const loginTotal = ref(0)
const loginLoading = ref(false)
let loginReqSeq = 0

/* ============ 审计日志 tab ============ */
async function loadAuditLogs() {
  auditPage.value = 1
  await loadAuditPage(1)
}

async function loadAuditPage(page) {
  const seq = ++auditReqSeq
  auditLoading.value = true
  try {
    const params = new URLSearchParams({ page, page_size: TAB_PAGE_SIZE })
    if (auditFilter.username) params.set('q', auditFilter.username)
    if (auditFilter.action) params.set('action', auditFilter.action)
    if (auditFilter.ip) params.set('ip', auditFilter.ip)
    if (auditFilter.startDate) params.set('start_date', auditFilter.startDate)
    if (auditFilter.endDate) params.set('end_date', auditFilter.endDate)
    const data = await api.getJson('/api/v1/audit/logs/?' + params.toString())
    // 请求序号守卫：快速连续翻页时丢弃旧响应，避免旧数据覆盖新状态
    if (seq !== auditReqSeq) return
    auditRows.value = data.rows || []
    auditTotal.value = data.total || 0
    // 数据量减少导致当前页越界时，回退到最后一页重新加载
    const totalPages = Math.max(1, data.total_pages || 1)
    const curPage = Math.min(page, totalPages)
    if (curPage !== page) {
      auditPage.value = curPage
      loadAuditPage(curPage)
      return
    }
  } catch (e) {
    if (seq !== auditReqSeq) return
    ElMessage.error('加载审计日志失败: ' + errMsg(e, '未知错误'))
  } finally {
    if (seq === auditReqSeq) auditLoading.value = false
  }
}

function resetAuditFilter() {
  auditFilter.username = ''
  auditFilter.action = ''
  auditFilter.ip = ''
  auditFilter.startDate = ''
  auditFilter.endDate = ''
  loadAuditLogs()
}

/* ============ IP 白名单 tab ============ */
async function loadWhitelist() {
  whiteLoading.value = true
  try {
    const data = await api.getJson('/api/v1/security/ip-whitelist/')
    whitelistCache.value = data.rows || []
  } catch (e) {
    ElMessage.error('加载白名单失败: ' + errMsg(e, '未知错误'))
  } finally {
    whiteLoading.value = false
  }
}

/* ============ IP 黑名单 tab ============ */
async function loadBlacklist() {
  blackLoading.value = true
  try {
    const data = await api.getJson('/api/v1/security/ip-blacklist/')
    blacklistCache.value = data.rows || []
    const totalPages = Math.max(1, Math.ceil(blacklistCache.value.length / TAB_PAGE_SIZE))
    if (blackPage.value > totalPages) blackPage.value = 1
  } catch (e) {
    ElMessage.error('加载黑名单失败: ' + errMsg(e, '未知错误'))
  } finally {
    blackLoading.value = false
  }
}

/* ============ 敏感词 tab ============ */
async function loadSensitive() {
  sensitiveLoading.value = true
  try {
    const data = await api.getJson('/api/v1/security/sensitive-words/')
    sensitiveCache.value = data.rows || []
  } catch (e) {
    ElMessage.error('加载敏感词失败: ' + errMsg(e, '未知错误'))
  } finally {
    sensitiveLoading.value = false
  }
}

/* ============ 登录尝试 tab ============ */
async function loadLoginAttempts() {
  loginPage.value = 1
  await loadLoginPage(1)
}

async function loadLoginPage(page) {
  const seq = ++loginReqSeq
  loginLoading.value = true
  try {
    const params = new URLSearchParams({ page, page_size: TAB_PAGE_SIZE })
    if (loginFilter.username) params.set('username', loginFilter.username)
    if (loginFilter.ip) params.set('ip', loginFilter.ip)
    if (loginFilter.result) params.set('result', loginFilter.result)
    const data = await api.getJson('/api/v1/security/login-attempts/?' + params.toString())
    // 请求序号守卫：快速连续翻页时丢弃旧响应
    if (seq !== loginReqSeq) return
    loginRows.value = data.rows || []
    loginTotal.value = data.total || 0
    // 数据量减少导致当前页越界时，回退到最后一页重新加载
    if (guardOverflow(loginTotal.value)) return
  } catch (e) {
    if (seq !== loginReqSeq) return
    ElMessage.error('加载登录记录失败: ' + errMsg(e, '未知错误'))
  } finally {
    if (seq === loginReqSeq) loginLoading.value = false
  }
}

function resetLoginFilter() {
  loginFilter.username = ''
  loginFilter.ip = ''
  loginFilter.result = ''
  loadLoginAttempts()
}

/* ============ tab 切换 ============ */
function onTabChange(tab) {
  if (tab === 'audit') loadAuditLogs()
  else if (tab === 'white') loadWhitelist()
  else if (tab === 'black') loadBlacklist()
  else if (tab === 'sensitive') loadSensitive()
  else if (tab === 'login') loadLoginAttempts()
}

/* ============ 前端分页切片（黑名单前端分页用） ============ */
function slicePage(items, page) {
  const start = (page - 1) * TAB_PAGE_SIZE
  return items.slice(start, start + TAB_PAGE_SIZE)
}
const blackPageItems = computed(() => slicePage(blacklistCache.value, blackPage.value))

/* ============ 展示辅助 ============ */
// 操作类型文案/标签色：由共享 makeStatusMeta 生成函数对（未知 op 原样展示，模板插值自动转义防 XSS）
const { label: opLabel, tagType: opTagType } = makeStatusMeta(OP_LABEL_MAP, OP_TAG_MAP, { labelFallback: '-', tagFallback: '' })
function resultLabel(result) {
  const m = RESULT_MAP[result] || RESULT_MAP.failed
  return m.label
}
function resultTagType(result) {
  const m = RESULT_MAP[result] || RESULT_MAP.failed
  return m.type
}
function sensitiveCategoryLabel(c) {
  return (SENSITIVE_CATEGORY_MAP[c] || {}).label || c || '-'
}
function sensitiveCategoryType(c) {
  return (SENSITIVE_CATEGORY_MAP[c] || {}).type || 'info'
}
function sensitiveActionLabel(a) {
  return (SENSITIVE_ACTION_MAP[a] || {}).label || a || '-'
}
function sensitiveActionType(a) {
  return (SENSITIVE_ACTION_MAP[a] || {}).type || 'info'
}
function blacklistReason(reason) {
  if (reason === 'login_fail') return '登录连续失败'
  if (reason === 'manual') return '人工封禁'
  return reason || '-'
}
function loginFailReason(result) {
  if (result === 'wrong_password') return '密码错误'
  if (result === 'user_not_found') return '用户不存在'
  if (result === 'locked') return '账户锁定'
  return '-'
}
function formatResource(targetType, targetId) {
  const type = targetType || ''
  const id = targetId ? String(targetId) : ''
  if (!type && !id) return '-'
  if (!id) return type
  return type + ': ' + id
}

/* ============ 审计详情弹窗 ============ */
const auditDetailVisible = ref(false)
const auditDetailRow = ref(null)
const auditDetailJson = ref('')

function showAuditDetail(row) {
  if (!row) return
  auditDetailRow.value = row
  auditDetailJson.value = JSON.stringify(row, null, 2)
  auditDetailVisible.value = true
}

async function copyAuditJson() {
  const text = auditDetailJson.value || ''
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      // 旧浏览器降级：隐藏 textarea + execCommand
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('已复制到剪贴板')
  } catch (e) {
    ElMessage.error('复制失败')
  }
}

/* ============ 白名单新增/编辑 ============ */
const whitelistDialogVisible = ref(false)
const whitelistEditId = ref(null)
const whitelistSaving = ref(false)
const whitelistFormRef = ref()
const whitelistForm = reactive({ ip_or_cidr: '', description: '' })
// IP 校验规则：必填 + validateIpPattern（单 IP / CIDR / 通配符 / 范围）
const whitelistIpRules = [
  { required: true, message: '请输入 IP 或 CIDR', trigger: 'blur' },
  {
    validator: (rule, value, callback) => {
      if (!value) return callback()
      const check = validateIpPattern(value)
      if (!check.valid) return callback(new Error(check.error))
      callback()
    }, trigger: 'blur',
  },
]

function openWhitelistDialog(row) {
  whitelistEditId.value = row ? row.id : null
  whitelistForm.ip_or_cidr = row ? row.ip_or_cidr : ''
  whitelistForm.description = row ? (row.description || '') : ''
  whitelistDialogVisible.value = true
}

async function saveWhitelist() {
  if (whitelistSaving.value) return
  try {
    await whitelistFormRef.value.validate()
  } catch {
    return
  }
  whitelistSaving.value = true
  try {
    const payload = { ip_or_cidr: whitelistForm.ip_or_cidr.trim(), description: whitelistForm.description.trim() }
    const res = whitelistEditId.value
      ? await api.put(`/api/v1/security/ip-whitelist/${whitelistEditId.value}/`, payload)
      : await api.postJson('/api/v1/security/ip-whitelist/', payload)
    whitelistDialogVisible.value = false
    if (res.status === 'executed') {
      ElMessage.success(whitelistEditId.value ? '白名单编辑已立即生效' : '白名单新增已立即生效')
    } else {
      ElMessage.info(`已创建审批工单 ${res.ticket_no}，需双审后生效`)
    }
    await loadWhitelist()
  } catch (e) {
    ElMessage.error(errMsg(e, whitelistEditId.value ? '更新失败' : '添加失败'))
  } finally {
    whitelistSaving.value = false
  }
}

function deleteWhitelist(id) {
  confirm({
    message: '删除白名单需双审，审批通过后生效',
    title: '删除白名单', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    const res = await api.deleteJson(`/api/v1/security/ip-whitelist/${id}/`)
    if (res.status === 'executed') {
      ElMessage.success('白名单已删除')
    } else {
      ElMessage.info(`已创建审批工单 ${res.ticket_no}，需双审后生效`)
    }
    await loadWhitelist()
  })
}

/* ============ 黑名单手动封禁 ============ */
const blacklistDialogVisible = ref(false)
const blacklistSaving = ref(false)
const blacklistFormRef = ref()
const blacklistForm = reactive({ ip: '', reason: '' })
const blacklistIpRules = [
  { required: true, message: '请输入 IP 地址', trigger: 'blur' },
  {
    validator: (rule, value, callback) => {
      if (!value) return callback()
      const check = validateIpPattern(value)
      if (!check.valid) return callback(new Error(check.error))
      callback()
    }, trigger: 'blur',
  },
]

function openBlacklistDialog() {
  blacklistForm.ip = ''
  blacklistForm.reason = ''
  blacklistDialogVisible.value = true
}

async function saveBlacklist() {
  if (blacklistSaving.value) return
  try {
    await blacklistFormRef.value.validate()
  } catch {
    return
  }
  blacklistSaving.value = true
  try {
    const res = await api.postJson('/api/v1/security/ip-blacklist/', {
      ip: blacklistForm.ip.trim(),
      reason: blacklistForm.reason.trim(),
      detail: '人工封禁',
    })
    blacklistDialogVisible.value = false
    if (res.status === 'executed') {
      ElMessage.success('黑名单新增已立即生效')
    } else {
      ElMessage.info(`已创建审批工单 ${res.ticket_no}`)
    }
    await loadBlacklist()
  } catch (e) {
    ElMessage.error(errMsg(e, '封禁失败'))
  } finally {
    blacklistSaving.value = false
  }
}

function unblockIp(id) {
  confirm({
    message: '解封需单审，审批通过后生效',
    title: '解封 IP', confirmText: '确认解封', type: 'info', errorText: '解封失败',
  }, async () => {
    const res = await api.put(`/api/v1/security/ip-blacklist/${id}/`, {})
    if (res.status === 'executed') {
      ElMessage.success('已解封')
    } else {
      ElMessage.info(`已创建审批工单 ${res.ticket_no}，需单审后生效`)
    }
    await loadBlacklist()
  })
}

/* ============ 敏感词 CRUD ============
 * 后端契约：
 *   POST   /api/v1/security/sensitive-words/        {word, category, action, is_regex}
 *   PUT    /api/v1/security/sensitive-words/{id}/   {action, is_enabled}（word/category 不可改）
 *   DELETE /api/v1/security/sensitive-words/{id}/
 * CRUD 后后端会自动触发 SensitiveFilter 重建（AC 自动机重载）
 */
const sensitiveDialogVisible = ref(false)
const sensitiveEditId = ref(null)
const sensitiveSaving = ref(false)
const sensitiveForm = reactive({ word: '', category: 'other', action: 'mask', is_regex: false, is_enabled: true })

function openSensitiveDialog(row) {
  sensitiveEditId.value = row ? row.id : null
  sensitiveForm.word = row ? (row.word || '') : ''
  sensitiveForm.category = row ? (row.category || 'other') : 'other'
  sensitiveForm.action = row ? (row.action || 'mask') : 'mask'
  sensitiveForm.is_regex = row ? !!row.is_regex : false
  sensitiveForm.is_enabled = row ? row.is_enabled !== false : true
  sensitiveDialogVisible.value = true
}

async function saveSensitive() {
  if (sensitiveSaving.value) return
  sensitiveSaving.value = true
  try {
    if (sensitiveEditId.value === null) {
      // 新增模式
      const word = sensitiveForm.word.trim()
      if (!word) {
        ElMessage.error('请输入敏感词')
        return
      }
      const res = await api.postJson('/api/v1/security/sensitive-words/', {
        word,
        category: sensitiveForm.category,
        action: sensitiveForm.action,
        is_regex: sensitiveForm.is_regex,
      })
      if (res.status === 'executed') ElMessage.success('敏感词新增已立即生效')
      else ElMessage.info(`已创建审批工单 ${res.ticket_no}`)
    } else {
      // 编辑模式：仅 action 和 is_enabled 可改
      const res = await api.put(`/api/v1/security/sensitive-words/${sensitiveEditId.value}/`, {
        action: sensitiveForm.action,
        is_enabled: sensitiveForm.is_enabled,
      })
      if (res.status === 'executed') ElMessage.success('敏感词变更已立即生效')
      else ElMessage.info(`已创建审批工单 ${res.ticket_no}，需单审后生效`)
    }
    sensitiveEditId.value = null // 状态收口：关闭前重置，防止残留
    sensitiveDialogVisible.value = false
    await loadSensitive()
  } catch (e) {
    ElMessage.error(errMsg(e, '保存失败'))
  } finally {
    sensitiveSaving.value = false
  }
}

function deleteSensitive(id) {
  confirm({
    message: '删除敏感词需单审，审批通过后生效',
    title: '删除敏感词', confirmText: '确认删除', errorText: '删除失败',
  }, async () => {
    const res = await api.deleteJson(`/api/v1/security/sensitive-words/${id}/`)
    if (res.status === 'executed') {
      ElMessage.success('敏感词已删除')
    } else {
      ElMessage.info(`已创建审批工单 ${res.ticket_no}，需单审后生效`)
    }
    await loadSensitive()
  })
}

/* ============ CSV 导出 ============ */
// 通用 CSV 下载：BOM 头与 Blob 构造统一走 utils/download 的 exportCsv

async function exportAuditLogs() {
  try {
    // 先取第一页获取 total，再分批拉取剩余页（page_size=200）
    const baseParams = []
    if (auditFilter.username) baseParams.push('q=' + encodeURIComponent(auditFilter.username))
    if (auditFilter.action) baseParams.push('action=' + encodeURIComponent(auditFilter.action))
    if (auditFilter.ip) baseParams.push('ip=' + encodeURIComponent(auditFilter.ip))
    if (auditFilter.startDate) baseParams.push('start_date=' + auditFilter.startDate)
    if (auditFilter.endDate) baseParams.push('end_date=' + auditFilter.endDate)

    let url = '/api/v1/audit/logs/?page=1&page_size=200'
    if (baseParams.length) url += '&' + baseParams.join('&')
    const first = await api.getJson(url)
    let rows = first.rows || []
    const totalPages = first.total_pages || 1

    for (let p = 2; p <= totalPages; p++) {
      url = '/api/v1/audit/logs/?page=' + p + '&page_size=200'
      if (baseParams.length) url += '&' + baseParams.join('&')
      const pageData = await api.getJson(url)
      rows = rows.concat(pageData.rows || [])
    }

    const header = '时间,用户,操作类型,资源,IP地址,结果\n'
    const csv = rows.map(r => [
      formatDate(r.created_at),
      (r.actor_username || '-').replace(/,/g, ' '),
      (r.action || '-').replace(/,/g, ' '),
      (r.target_type || '') + (r.target_id ? ':' + r.target_id : ''),
      (r.ip_address || '-').replace(/,/g, ' '),
      r.result || '-',
    ].join(',')).join('\n')
    exportCsv('audit_logs_' + new Date().toISOString().slice(0, 10) + '.csv', header + csv)
    ElMessage.success(`导出 ${rows.length} 条记录`)
  } catch (e) {
    ElMessage.error('导出失败: ' + errMsg(e, '未知错误'))
  }
}

async function exportLoginAttempts() {
  try {
    const baseParams = []
    if (loginFilter.username) baseParams.push('username=' + encodeURIComponent(loginFilter.username))
    if (loginFilter.ip) baseParams.push('ip=' + encodeURIComponent(loginFilter.ip))
    if (loginFilter.result) baseParams.push('result=' + encodeURIComponent(loginFilter.result))

    let url = '/api/v1/security/login-attempts/?page=1&page_size=200'
    if (baseParams.length) url += '&' + baseParams.join('&')
    const first = await api.getJson(url)
    let rows = first.rows || []
    const totalPages = Math.ceil((first.total || 0) / 200) || 1

    for (let p = 2; p <= totalPages; p++) {
      url = '/api/v1/security/login-attempts/?page=' + p + '&page_size=200'
      if (baseParams.length) url += '&' + baseParams.join('&')
      const pageData = await api.getJson(url)
      rows = rows.concat(pageData.rows || [])
    }

    // 结果中文映射
    const resultLabelMap = { success: '成功', wrong_password: '密码错误', user_not_found: '用户不存在', locked: '账户锁定', captcha_fail: '验证码失败', ip_denied: 'IP 拒绝' }
    const header = '时间,用户,IP地址,User-Agent,结果\n'
    const csv = rows.map(r => [
      formatDate(r.created_at),
      (r.username || '-').replace(/,/g, ' '),
      (r.ip || '-').replace(/,/g, ' '),
      (r.user_agent || '-').replace(/,/g, ' ').replace(/"/g, '""'),
      resultLabelMap[r.result] || r.result || '-',
    ].map(v => `"${v}"`).join(',')).join('\n')
    downloadCsv('login_attempts_' + new Date().toISOString().slice(0, 10) + '.csv', header, csv)
    ElMessage.success(`导出 ${rows.length} 条登录记录`)
  } catch (e) {
    ElMessage.error('导出失败: ' + errMsg(e, '未知错误'))
  }
}

/* ============ 初始化 ============ */
onMounted(async () => {
  await loadAuditLogs()
})
</script>

<style scoped>
/* ===== 页头与卡片 ===== */
/* 页头内容上下排列：标题在上、描述在下（参考 admin-eval 页头） */
.audit-page-head {
  flex-shrink: 0;
}

/* 覆盖全局 .app-card 的底部 margin，避免卡片下方出现多余留白 */
.audit-card {
  display: flex;
  flex-direction: column;
  padding: 4px 16px 16px;
  margin-bottom: 0;
  min-height: 0;
  flex: 1;
}
/* el-tabs 三件套（撑满 + 面板内部滚动）由全局 .tabs-fill 提供 */

/* 表格滚动区：占满面板剩余高度，表头固定、表体内部上下滚动 */
.tab-table-wrap {
  flex: 1;
  min-height: 0;
}

/* ===== 筛选栏 ===== */
.audit-filter-bar {
  flex-shrink: 0;
  margin-bottom: 12px;
  padding: 10px 12px;
  background: var(--app-menu-hover);
  border-radius: 6px;
  border: 1px solid var(--app-border);
}

.filter-spacer {
  flex: 1;
}

.ip-code {
  background: var(--app-bg);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

/* ===== tab 顶部信息条 + 操作按钮 ===== */
.tab-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-shrink: 0;
}

.tab-info-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  background: #f0f4ff;
  border-left: 3px solid #409eff;
  border-radius: 0 6px 6px 0;
  font-size: 13px;
  color: var(--app-text-sub);
  line-height: 1.6;
  flex: 1;
  min-width: 0;
}

.tab-info-bar .tab-info-icon {
  font-size: 15px;
  flex-shrink: 0;
}

.tab-info-bar .tab-info-count {
  font-weight: 600;
  color: #409eff;
  margin-left: 2px;
}

/* 深色模式下信息条改为深色底，避免浅蓝背景突兀 */
html.dark .tab-info-bar {
  background: rgba(64, 158, 255, 0.08);
  border-left-color: #409eff;
}

.login-info {
  margin-bottom: 12px;
  flex: none; /* 登录 tab 中信息条是纵向 flex 子项，禁止被拉伸 */
}

.danger-text {
  color: #f56c6c;
}

.dlg-banner {
  margin-bottom: 14px;
}

.danger-banner {
  background: #fef0f0;
  border-left-color: #f56c6c;
}

/* 深色模式下危险提示条同步降亮 */
html.dark .danger-banner {
  background: rgba(245, 108, 108, 0.1);
  border-left-color: #f56c6c;
}

/* ===== 表格与分页 ===== */
.audit-table {
  width: 100%;
}

.tab-pagination {
  flex-shrink: 0;
  margin-top: 14px;
  justify-content: flex-end;
}

/* ===== 审计详情弹窗 ===== */
/* .detail-grid/.detail-block/.detail-label/.detail-value 为全局公共类（assets/style.css），此处仅保留页面特有样式 */

.detail-pre {
  background: #0f172a;
  color: #e2e8f0;
  padding: 12px 14px;
  border-radius: 6px;
  font-family: 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  overflow-x: auto;
  max-height: 320px;
  overflow-y: auto;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
}

/* ===== 敏感词动作说明条 ===== */
.sensitive-action-guide {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 16px;
  padding: 10px 12px;
  background: var(--app-menu-hover);
  border-radius: 6px;
  border: 1px solid var(--app-border);
}

.sensitive-action-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--app-text-sub);
}

.sensitive-action-badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
  font-family: 'Courier New', monospace;
  white-space: nowrap;
}

.sensitive-action-block .sensitive-action-badge {
  background: rgba(239, 68, 68, .12);
  color: #dc2626;
}

.sensitive-action-mask .sensitive-action-badge {
  background: rgba(245, 158, 11, .12);
  color: #d97706;
}

.sensitive-action-warn .sensitive-action-badge {
  background: rgba(59, 130, 246, .12);
  color: #2563eb;
}
</style>
