# 直接使用

在 `/home/liyuhao/Documents/Projects/literature-ai` 运行：

```bash
/usr/bin/python3 scripts/litai.py doctor
/usr/bin/python3 scripts/litai.py catalog --filter /api/rebuild
/usr/bin/python3 scripts/litai.py get '/api/rebuild/papers?q=关键词&limit=20&offset=0'
/usr/bin/python3 scripts/litai.py get '/api/rebuild/library-progress?library_name=%E9%94%82%E7%A1%AB%E5%8F%8C%E5%8E%9F%E5%AD%90' --out outputs/library-progress.json
/usr/bin/python3 scripts/litai.py get '/api/rebuild/papers/PAPER_UUID/ai-work-package?view=compact' --out outputs/work-package.json
/usr/bin/python3 scripts/litai.py get '/api/rebuild/papers/PAPER_UUID/files'
/usr/bin/python3 scripts/litai.py get '/api/papers/PAPER_UUID/datasets'
/usr/bin/python3 scripts/litai.py get '/api/rebuild/papers/PAPER_UUID/delivery-package?view=summary'
/usr/bin/python3 scripts/litai.py get '/api/rebuild/files/FILE_UUID/text-pages?offset=0&limit=5'
/usr/bin/python3 scripts/litai.py download '/api/rebuild/files/FILE_UUID/render-page?page=1&dpi=120' --out outputs/page-1.png
/usr/bin/python3 scripts/litai.py download '/api/rebuild/files/FILE_UUID/download' --out outputs/original.pdf
/usr/bin/python3 scripts/litai.py download '/api/papers/PAPER_UUID/datasets/DATASET_UUID/files/DATASET_FILE_UUID' --out outputs/dataset-source.bin
/usr/bin/python3 scripts/litai.py download '/api/papers/PAPER_UUID/datasets/DATASET_UUID/rows.csv' --out outputs/dataset-rows.csv
/usr/bin/python3 scripts/litai.py schema POST /api/rebuild/rows/import --help
/usr/bin/python3 scripts/litai.py schema POST /api/rebuild/rows/import > outputs/import-contract.json
/usr/bin/python3 scripts/litai.py request POST /api/rebuild/rows/import --json outputs/rows.json --task stable-run-id
/usr/bin/python3 scripts/litai.py request POST /api/rebuild/papers/PAPER_UUID/assets/import --json outputs/assets.json --task stable-run-id
/usr/bin/python3 scripts/litai.py request PUT /api/rebuild/papers/PAPER_UUID/processing-status --json outputs/status.json --task stable-run-id
/usr/bin/python3 scripts/litai.py raw-upload '/api/papers/PAPER_UUID/datasets/DATASET_UUID/files/SHA256' --file 'outputs/原始 数据 文件.xlsx' --task stable-run-id
```

`schema` 接受真实 UUID 路径或带 `{paper_id}` 的模板路径，返回操作合同与引用 schemas。业务 JSON 从文件或 `--json -` 标准输入读取。示例 UUID 须替换，URL 查询字符串须编码。

## 读写和恢复

- get 读取 JSON，可用 --out 保存完整内容再按字段检查；不能把首个分页当全量。
- download 支持 PDF、PNG、CSV 等二进制。只保存到项目目录，自动生成 `.source.json`（原接口、字节数、SHA256 和服务器来源哈希）。图片用 Codex 图片工具打开，不以文本抽取替代看图。
- request 支持 POST、PUT、PATCH、DELETE 的 JSON 请求，适用于业务接口，不限于普通解析端点。先读合同，不给错误接口随意添加字段。multipart 文件使用 `upload`；数据集文件的原始字节 PUT 使用 `raw-upload`，不能误用 JSON request。
- request 自动生成稳定 X-Request-ID 与 X-Task-ID，业务 JSON 已有 request_id 时优先沿用，不能 header/body 冲突。会写入 outputs/requests 下请求、原字节响应及错误/未知回执。相同请求再次调用会向服务器读回原幂等回执，不把本地旧 receipt 当当前数据库结果。
- 对于需要 UUID 型 request_id 的接口，在 JSON 中显式提供 UUID，客户端会原样沿用。外部 run_id 自己生成并贯穿同篇尝试。
- rows/import 自动预检，preflight.complete=false 时默认停在预检。可先修正失败字段，也可显式 --allow-partial 提交合法部分；检查服务器逐项结果。
- GET 和 rows/preflight 使用最多三次退避重试；其他写操作不自动重试。连接中断等未取得明确 HTTP 结果时保存 unknown 回执。写请求若经代理返回 502/504，也可能已经在服务端提交，必须按请求身份和对象读回，不能先换请求体重试。只在接口提供幂等保证时执行 `replay <原.request.json>`。
- 成功写入后仍需 GET 资产/行/字段核对。分析、导出、单元格修正、来源修正、文件注册及生命周期等都使用同一个 request 命令，具体请求形状见生产 schema。

## multipart 上传

```bash
/usr/bin/python3 scripts/litai.py schema POST /api/rebuild/papers/PAPER_UUID/files
/usr/bin/python3 scripts/litai.py upload /api/rebuild/papers/PAPER_UUID/files --file outputs/SI.pdf --file-field file --field role=si --field request_id=REQUEST_UUID --field expected_revision=FILE_SET_REVISION --field action=add --field reason='supplementary source' --task stable-run-id
```

`REQUEST_UUID` 和 `FILE_SET_REVISION` 必须来自本次请求及刚读取的文件集，不能复用示例值。以实际 schema 要求的字段为准。上传保留原 multipart 字节和请求记录，未知响应不自动再传。body.multipart 和 request.json 含原文件资料，但不含登录密码或 Cookie。

数据集文件的上传合同是 `PUT /api/papers/{paper_id}/datasets/{dataset_id}/files/{sha256}`，请求体为文件原字节，文件身份由路径 SHA-256 和查询参数 `filename`、`size_bytes`、可选 `mime_type` 构成。`upload` 只处理 multipart；这条接口使用下一节的 `raw-upload`。调用前先以 `schema PUT ...` 核对，并保留原文件及请求回执。

## 数据集原始字节上传

```bash
/usr/bin/python3 scripts/litai.py raw-upload \
  '/api/papers/PAPER_UUID/datasets/DATASET_UUID/files/SHA256' \
  --file 'outputs/原始 数据 文件.xlsx' \
  --task stable-run-id \
  --out outputs/raw-upload-result.json
```

`raw-upload` 只接受上述数据集文件路径，路径不能再夹带查询参数。客户端从 `--file` 原样读取字节，自动取 basename、字节数、SHA-256 和 MIME（无法识别时为 `application/octet-stream`），用 UTF-8 百分号编码生成 `filename`、`size_bytes`、`mime_type` 查询参数。发送前会核对文件实际 SHA-256 与路径末段；文件不存在或哈希不符时不会发出业务请求。请求体仅含原始字节，使用 `PUT`、同源 Cookie、Origin 和 CSRF，并带稳定的 `X-Request-ID`、`X-Task-ID`。

请求记录保存在 `outputs/requests/<seed>/`，其中 request 回执记录方法、无查询路径、查询字符串、文件大小、SHA-256、请求身份和源文件；响应及 receipt 单独保存。日志不包含密码、Cookie 或 CSRF token。写请求不自动重试。超时或断连会保存 `.unknown.json`；读取服务端原对象后确需重放时，使用 `replay <原.request.json>`。raw replay 只接受已存在的规范路径 `outputs/requests/<seed>/<request_id>.request.json`，并在网络请求前核对字段集合、seed 目录、文件名、请求 ID、服务地址、完整请求描述、源文件路径与实际字节，以及同目录下相匹配的 `.receipt.json` 或 `.unknown.json`。任一字段被修改、原记录缺失、请求记录没有终态旁证或旧记录无法安全重建时都会拒绝；客户端不会迁移旧记录或把 replay 退化为首次上传。

`replay` 不再根据 `mode` 或零散的 raw 字段猜测类型。输入必须恰好匹配完整 raw-upload envelope 或完整 JSON envelope；其他字段组合直接拒绝。JSON replay 同样先核对规范 `outputs/requests/<seed>/<request_id>.request.json` 路径、自身 body 计算的 seed、request ID、完整 envelope 和 receipt/unknown 旁证，再由 `mutate` 以 `require_existing` 方式发送；原记录缺失时不会新建 request。

`request` 用于 JSON 包装体，`upload` 用于 multipart 表单，`raw-upload` 用于请求体就是文件原字节的接口。三者不可互换。

## 会话和权限

首次调用自动登录，401 自动续登；登录失败如实报错。Origin/CSRF 与工作台同源，不跳过服务器校验，TLS 使用系统证书校验。客户端不经环境 HTTP_PROXY，直接连已配置 HTTPS 工作台。

权限等同现有工作台账号，未另设文献库/论文/写方法限制。完整 API 能力不等于所有接口在任何状态都可成功：来源哈希、版本冲突、同篇占用和独立科学验收授权仍由服务器校验。API 不提供的功能不能靠修改客户端绕过。
