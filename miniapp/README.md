# 小程序前端（Phase 06）

微信原生小程序：登录 → 上传答卷 PDF → 报价 → 支付 → 观察批改进度 → 下载结果 →
验收 / 复核 / 退款。

**服务端是唯一权威。** 小程序不自行计算价格、退款资格、可执行动作或订单状态，
也**不把支付 UI 的成功回调当作已支付**。

## 目录结构

| 路径 | 作用 |
|---|---|
| `app.js` / `app.json` / `app.wxss` | 入口、路由与全局样式；在这里装配唯一的 API 客户端与各 service |
| `config.js` | 环境档案（`staging` / `production`），切换假登录/假支付与真实微信路径 |
| `services/api.js` | 唯一 HTTP 出口：Bearer 头、30 秒超时、`ApiError(status, detail)`、401 钩子 |
| `services/session.js` | 会话与设备身份持久化（原始 token 只存本地，不写日志） |
| `services/auth.js` | `ensureLogin()`：先复用会话并用 `GET /api/v1/me` 校验，失效才重新登录 |
| `services/quotes.js` | 创建报价；一个文件走 `wx.uploadFile`，两个文件走自建 multipart |
| `services/payments.js` | 预支付 + **轮询服务端确认**；`PaymentUnconfirmed` 表示未确认 |
| `services/orders.js` | 订单分页、详情与列表进度轮询（15 秒、可见时轮询、stop 时真正清 timer） |
| `services/downloads.js` | 结果下载与结果摘要读取；401/403/410 触发刷新 |
| `services/aftersales.js` | 验收 / 复核 / 退款；动作只来自 `available_actions` |
| `utils/` | 格式化、订单状态词表、multipart 编码 |
| `components/` | `pdf-picker`、`price-summary`、`order-card`、`status-pill` |
| `pages/` | `home` `account` `create/{upload,options,payment}` `orders/{index,detail}` `aftersales/{review,refund}` |

## 运行测试

```bash
cd miniapp
npm test          # Node 内置 node:test，无需任何第三方框架，不需要微信开发者工具
```

当前 **134 项通过**。测试全部在纯 Node 下运行：`wx.request` / `wx.uploadFile` /
`wx.downloadFile` 都通过依赖注入传入，源码里不依赖真实 `wx` 全局对象。

`tests/structure.test.js` 是对源码的结构断言，用于守住几条不能回退的约定：
复核页不得出现文件选择器、退款页不得有金额输入、任何页面不得计算金额、
不得出现密钥或 `/worker/v1/*` `/admin/api/*` 调用、轮询必须在 `onHide` 与
`onUnload` 都停止。

## 手动真机验收清单

**这一步需要你本人操作**：需要微信开发者工具、一个小程序测试号，以及本机启动的服务端。
自动化测试无法覆盖真机行为（文件选择、支付面板、`wx.openDocument`），所以下面 6 步
**尚未验证**。

### 准备工作（需要你做）

1. 启动服务端（仓库根目录）：

   ```bash
   .venv/bin/python -c 'import uvicorn; from server.config import ServerSettings; from server.main import create_app; uvicorn.run(create_app(ServerSettings()), host="127.0.0.1", port=8000)'
   ```

   确认 `curl http://127.0.0.1:8000/health/ready` 返回 `{"database":"ok","storage":"ok"}`。

   `.env` 需要 `GRADER_ENVIRONMENT=staging`（或 `development`），否则假登录与假支付
   **不会注册**，登录会直接 404。

2. 可选，验证自动验收与过期清理时才需要：

   ```bash
   .venv/bin/python -m server.scheduler.main
   ```

3. 需要一个真实 Worker 才能产生批改结果。没有 Worker 时订单会停在 `v1_queued`，
   第 4–6 步无法进行。Worker 侧需要 Codex CLI 与 XeLaTeX。

4. 微信开发者工具：导入 `miniapp/` 目录，填入你的测试号 AppID
   （**不要提交** `project.private.config.json`），并勾选
   **不校验合法域名、web-view、TLS 证书**（`project.config.json` 已设 `urlCheck: false`，
   但真机预览仍需在工具里确认）。

5. 真机中的 `127.0.0.1` 指向手机自身，不能直接访问 Mac。真机调试使用隔离的
   HTTPS Quick Tunnel；不要修改默认 staging 地址，也不要把随机地址提交到 Git：

   ```bash
   ./tmp/start-device-tunnel.command
   ```

   脚本确认本地 `/health/ready` 后，会打印一个临时
   `https://*.trycloudflare.com` 地址和对应的启动参数。在微信开发者工具中新建
   “真机 HTTPS 调试”自定义编译条件，把脚本打印的整行参数粘贴到“启动参数”，
   然后使用“真机调试”。普通编译没有这些参数，仍固定使用
   `http://127.0.0.1:8000`。

   先在手机 Safari 打开脚本打印的 `/health/ready` 地址；能看到数据库和存储正常
   后再测试小程序。保持 Tunnel 终端运行，测试完成后按 `Control-C`。Quick Tunnel
   是临时公网入口，仅上传测试文件。若微信明确提示 `url not in domain list`，当前
   调试模式不接受随机域名，需要改用已配置到微信后台的稳定 HTTPS 测试域名。

### 六步验收

| # | 步骤 | 预期结果 | 失败时的排查方向 |
|---|---|---|---|
| 1 | 打开小程序 | 首页显示引导（新账号）或订单概览；「我的」页显示 `u-` 开头的用户标识 | 400 → `.env` 不是 staging/development，或 code 不以 `test-` 开头；401 → 检查 `GET /api/v1/me`；网络失败 → `baseUrl` 与域名跳过设置 |
| 2 | 「批改」→ 选择答卷 PDF，可选再选参考答案 | 显示文件名与大小；答卷必选，参考答案标注「不计价」 | 选不到文件 → 需从聊天记录选择（`chooseMessageFile`）；请先把 PDF 发给「文件传输助手」 |
| 3 | 选赛制 → 上传获取报价 → 确认支付 | 报价页显示页数、单价、合计；**页数只由答卷决定**；支付后跳转订单详情且状态为排队中 | 400 → PDF 加密/损坏/超页数；上传中离开会被拦截提示；报价过期需重新上传 |
| 4 | 等待 Worker 批改 | 列表显示短进度、详情显示完整进度，依次覆盖读取答卷、理解题目、核验、评分、报告与上传，最终变为待验收；实际批改期间圆点缓慢呼吸 | 一直排队 → 没有在线 Worker；显示「系统处理中」→ 任务 `worker_exception`，需查 Worker 日志 |
| 5 | 详情页下载结果 PDF | 系统阅读器打开批改 PDF；页数应为答卷页数 + 1；摘要区显示总分 | 410 → 该订单已退款；404 → 轮次未交付；打不开 → `wx.openDocument` 需要 `fileType: 'pdf'` |
| 6 | 申请复核 → 交付后申请退款 | 复核页**没有文件选择入口**且显示原答卷信息，提交后订单进入复核排队；退款页只显示全额、不能改金额；退款成功后**再次下载被拒（410）** | 复核按钮不出现 → `available_actions` 不含 `review`（V2 没有复核）；退款后仍能下载 → 说明 `downloads_revoked_at` 未被检查，属严重缺陷 |

第 6 步的最后一项是 Phase 05「退款后撤销下载」这条安全不变量的**唯一端到端验证点**，
请务必实际点一次下载确认被拒绝。自动化测试已覆盖该逻辑
（`tests/server/test_result_downloads.py::test_download_is_denied_after_a_refund_revokes_it`），
但真机链路只有你能确认。

## 尚未实现 / 已知限制

- **真实微信登录与真实微信支付未接**。代码里两条分支都存在
  （`config.js` 的 `auth` / `payment`），但只有假路径经过实际调用验证。
  接真实支付时必须给假回调加签名校验。
- 支付后靠「轮询订单列表里出现新订单」来确认，因为服务端没有「按 quote 查订单」的接口。
  同一账号在别处并发下单极端情况下可能误认；本阶段刻意不为此扩展服务端 API。
- 结果摘要从 `result_json` 解析，字段缺失时只是不显示摘要，不影响下载。
- 未做骨架屏、图片压缩、分享卡片等体验优化。
