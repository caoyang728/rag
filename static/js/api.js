/* ==========================================================
   知库 Agent · 统一 API 请求服务 (api.js)
   包含：token 管理、自动刷新、请求封装、SSE 流式
   依赖：common.js（内部 logout 调用 toast）；需在 common.js 之后、页面 js 之前加载
   login / reset-password 不引入（用原生 fetch）
   ========================================================== */

/* ============ 统一 API 请求服务 ============ */
const api = {
	baseUrl: '/api/v1',
	isRefreshing: false,
	refreshSubscribers: [],

	getToken() {
		return localStorage.getItem('rag_access');
	},

	getRefreshToken() {
		return localStorage.getItem('rag_refresh');
	},

	async refreshToken() {
		const refresh = this.getRefreshToken();
		if (!refresh) {
			throw new Error('No refresh token');
		}

		const response = await fetch('/api/v1/auth/token/refresh/', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ refresh })
		});

		if (!response.ok) {
			throw new Error('Refresh failed');
		}

		const data = await response.json();
		localStorage.setItem('rag_access', data.access);
		if (data.refresh) {
			localStorage.setItem('rag_refresh', data.refresh);
		}
		return data.access;
	},

	enqueueRefresh(callback) {
		return new Promise((resolve, reject) => {
			this.refreshSubscribers.push({ resolve, reject, callback });
		});
	},

	async handleRefresh() {
		try {
			const newToken = await this.refreshToken();
			this.refreshSubscribers.forEach(sub => {
				try {
					sub.resolve(newToken);
				} catch (e) {
					sub.reject(e);
				}
			});
		} catch (e) {
			this.refreshSubscribers.forEach(sub => sub.reject(e));
			this.logout();
		} finally {
			this.isRefreshing = false;
			this.refreshSubscribers = [];
		}
	},

	logout() {
		toast('登录已过期，请重新登录', 'error');
		localStorage.removeItem('rag_access');
		localStorage.removeItem('rag_refresh');
		localStorage.removeItem('rag_user');
		setTimeout(() => { window.location.href = '/login/'; }, 1500);
	},

	_formatError(data) {
		if (!data) return '请求失败';
		// DRF 异常处理器将 PermissionDenied / Http404 等包装为
		// {code, message, details: {detail: "原始错误信息"}}，
		// 其中 details 仅含单个 detail 键，属于业务错误而非字段校验错误，
		// 直接返回该错误信息即可（避免拼出 "detail: xxx" 的歧义格式）。
		if (data.details && typeof data.details === 'object'
			&& 'detail' in data.details && Object.keys(data.details).length === 1) {
			return data.details.detail || data.message || '请求失败';
		}
		// 字段校验错误（details 含多个字段名 → 错误列表）
		if (data.details && typeof data.details === 'object') {
			const msgs = [];
			for (const [field, errors] of Object.entries(data.details)) {
				const errList = Array.isArray(errors) ? errors : [errors];
				for (const e of errList) {
					const key = `${field}:${e}`;
					// 常见字段错误映射
					const map = {
						'email:具有 email 的 user 已存在。': '该邮箱已被使用',
						'username:具有 username 的 user 已存在。': '该用户名已被使用',
					};
					msgs.push(map[key] || `${field}: ${e}`);
				}
			}
			return msgs.join('；');
		}
		return data.detail || data.message || '请求失败';
	},

	async handleError(res) {
		if (!res.ok) {
			let detail = '请求失败';
			let data = null;
			try {
				data = await res.json();
				detail = this._formatError(data);
			} catch (e) {
				if (res.status === 403) detail = '无权限访问此资源';
			}
			// 错误提示统一交给调用方 catch 处理，避免 handleError 内部 toast 与调用方 catch 造成双重告警
			// 同时挂载 status/data 供调用方做条件分支（如 409 恢复用户场景）
			const err = new Error(detail);
			err.status = res.status;
			err.data = data;
			throw err;
		}
		return res;
	},

	async fetchWithAuth(method, url, options = {}) {
		const token = this.getToken();
		const headers = {
			'Content-Type': 'application/json',
			...options.headers
		};
		if (token) {
			headers['Authorization'] = `Bearer ${token}`;
		}

		return fetch(url, {
			method: method.toUpperCase(),
			headers,
			body: options.body,
			...options
		});
	},

	async request(method, url, options = {}) {
		let response = await this.fetchWithAuth(method, url, options);

		if (response.status === 401) {
			if (!this.isRefreshing) {
				this.isRefreshing = true;
				this.handleRefresh();
			}

			await new Promise((resolve, reject) => {
				this.refreshSubscribers.push({
					resolve: () => resolve(),
					reject: (err) => reject(err)
				});
			});

			response = await this.fetchWithAuth(method, url, options);
		}

		return this.handleError(response);
	},

	async get(url, options = {}) {
		return this.request('GET', url, options);
	},

	async post(url, data, options = {}) {
		return this.request('POST', url, {
			...options,
			body: typeof data === 'string' ? data : JSON.stringify(data)
		});
	},

	async put(url, data, options = {}) {
		return this.request('PUT', url, {
			...options,
			body: typeof data === 'string' ? data : JSON.stringify(data)
		});
	},

	async patch(url, data, options = {}) {
		return this.request('PATCH', url, {
			...options,
			body: typeof data === 'string' ? data : JSON.stringify(data)
		});
	},

	async delete(url, options = {}) {
		return this.request('DELETE', url, options);
	},

	async stream(url, data, onChunk, options = {}) {
		let token = this.getToken();
		const headers = {
			'Content-Type': 'application/json',
			...options.headers
		};
		if (token) {
			headers['Authorization'] = `Bearer ${token}`;
		}

		let response = await fetch(url, {
			method: 'POST',
			headers,
			body: typeof data === 'string' ? data : JSON.stringify(data),
			...options
		});

		if (response.status === 401) {
			if (!this.isRefreshing) {
				this.isRefreshing = true;
				this.handleRefresh();
			}

			token = await new Promise((resolve, reject) => {
				this.refreshSubscribers.push({
					resolve: (newToken) => resolve(newToken),
					reject: (err) => reject(err)
				});
			});

			headers['Authorization'] = `Bearer ${token}`;
			response = await fetch(url, {
				method: 'POST',
				headers,
				body: typeof data === 'string' ? data : JSON.stringify(data),
				...options
			});
		}

		if (!response.ok) {
			await this.handleError(response);
			return;
		}

		const reader = response.body.getReader();
		const decoder = new TextDecoder('utf-8');
		let buffer = '';
		let streamDone = false;

		// WARNING: 收到 [DONE] 标记后必须主动结束读取。
		// 某些服务器（如 Django dev server）在 StreamingHttpResponse 迭代完后
		// 不一定及时关闭连接，导致 reader.read() 永不返回 done=true，
		// 调用方 await api.stream(...) 会一直挂起（isSending 卡死）。
		try {
			while (!streamDone) {
				const { done, value } = await reader.read();
				if (done) break;

				buffer += decoder.decode(value, { stream: true });
				const lines = buffer.split('\n');
				buffer = lines.pop() || '';

				for (const line of lines) {
					if (line.trim().startsWith('data: ')) {
						const jsonStr = line.slice(6);
						if (jsonStr.trim() === '[DONE]') { streamDone = true; break; }
						try {
							const chunk = JSON.parse(jsonStr);
							onChunk(chunk);
						} catch (e) {
							console.warn('Failed to parse SSE chunk:', e);
						}
					}
				}
			}
		} finally {
			// 主动释放 reader，避免连接悬挂
			try { reader.cancel(); } catch (e) { /* ignore */ }
		}
	},

	async getJson(url, options = {}) {
		const res = await this.get(url, options);
		const ct = res.headers.get('content-type') || '';
		if (ct.includes('text/csv')) return res.blob();
		if (res.status === 204) return null;
		return res.json();
	},

	async postJson(url, data, options = {}) {
		const res = await this.post(url, data, options);
		if (res.status === 204) return null;
		return res.json();
	},

	async patchJson(url, data, options = {}) {
		const res = await this.patch(url, data, options);
		if (res.status === 204) return null;
		return res.json();
	},

	async deleteJson(url, options = {}) {
		const res = await this.delete(url, options);
		if (res.status === 204) return null;
		return res.json();
	}
};
