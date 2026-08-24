# 腾讯电子签转交

本模式把已确认的 PDF 上传至腾讯电子签，并生成由用户亲自在腾讯电子签小程序中检查和发起的入口。它不是自动签名、自动用印或 OA 审批。

## 能力边界

只允许使用 `scripts/tencent_esign.py` 的四个命令：

- `auth-check`：确认当前调用或部署环境是否提供凭证，不联网、不显示凭证。
- `upload`：把一份 PDF 上传至腾讯电子签，返回 `ResourceId`。
- `prepare`：根据 `ResourceId` 生成小程序发起链接。
- `handoff`：在一次脚本调用中依次执行 `upload` 和 `prepare`，这是聊天 Token 模式的首选命令。

禁止尝试通用 API、自动签署、印章管理、撤销合同、解除协议、批量发起或替用户填写签署身份。可以接收用户在当前聊天中主动提供的 Token，但不得执行保存 Token 的命令。

## 前置条件

同时满足以下条件才能继续：

- 用户明确要求把合同交给电子签流程，而不只是询问电子签知识。
- 已定位一份完整 PDF；如果原文件是 Word，只能使用宿主已真实转换并可定位的 PDF，不能把 Markdown 或纯文本伪装成 PDF。
- 文件名、版本、合同主体和签署用途已确认。
- 文件不含 `【待确认】`、`【待协商】`、空白核心交易条件、未缓释红色风险或影响签署安全的关键灰色事项。
- 用户有权处理合同及其中的个人信息、商业秘密和交易数据，并同意将该文件发送给腾讯电子签。
- 用户已在当前会话提供腾讯电子签 Token，或部署管理员已通过环境变量 `ESIGN_TOKEN` 配置凭证。

不满足任何一项时停止转交，说明阻断项和修复方式。不要为了完成签署而自动修改合同、补数字或降低风险等级。

## 执行流程

### 获取凭证

如果用户尚未提供 Token，可先运行：

```text
run_script(
  name="contract-legal-expert",
  rel_path="scripts/tencent_esign.py",
  args=["auth-check"]
)
```

若返回未配置，请用户从腾讯电子签页面复制 Token 并粘贴到当前聊天。用户只需发送一次；把 Token 视为不透明字符串，不解释、不改写、不截断、不复述，也不写入任何文件。部署环境已有 `ESIGN_TOKEN` 时仍可沿用，无需用户再次提供。

### 最终人工确认

上传前向用户展示准确文件名和版本，并单独询问：

> 确认将《文件名》发送至腾讯电子签并生成小程序发起链接吗？文件将离开当前 LazyMind 环境；请同时确认你有权处理其中的个人信息和商业秘密。

必须等待用户明确同意。此前对“可以签”“准备签”的一般表述不能替代这次确认。

### 一次完成上传与入口生成

从当前附件上下文中的 `Source path` 取得真实绝对路径，不猜测路径。用户在聊天中提供 Token 时，最终确认后首选一次调用 `handoff`：

```text
run_script(
  name="contract-legal-expert",
  rel_path="scripts/tencent_esign.py",
  args=[
    "handoff",
    "--file", "/absolute/path/contract.pdf",
    "--flow-name", "contract.pdf",
    "--token", "用户刚刚提供的完整 Token"
  ]
)
```

`args` 必须是字符串数组。不要给整个数组或其中任何元素额外套一层序列化字符串。脚本只接受单份 PDF，并限制文件大小以适配 LazyMind 当前脚本执行时限。

若使用部署环境中的 `ESIGN_TOKEN`，省略最后两个 `--token` 参数即可。成功时读取 `Response.ResourceId` 和 `Response.LongUrl`；缺少任一字段都不得声称已生成入口。

### 分步恢复

只有 `handoff` 失败或需要恢复已上传文件时，才分步调用。上传：

```text
run_script(
  name="contract-legal-expert",
  rel_path="scripts/tencent_esign.py",
  args=["upload", "--file", "/absolute/path/contract.pdf", "--token", "完整 Token"]
)
```

再使用真实 `ResourceId` 生成入口：

```text
run_script(
  name="contract-legal-expert",
  rel_path="scripts/tencent_esign.py",
  args=[
    "prepare",
    "--resource-id", "真实 ResourceId",
    "--flow-name", "原始文件名",
    "--token", "同一完整 Token"
  ]
)
```

原样展示响应中的 `LongUrl`。说明该链接只是腾讯电子签的发起入口，用户仍需在腾讯页面核对合同、签署方、签章位置及其他信息，并亲自确认发起和签署。不得改写、截断或拼接 URL。

## 失败与停止条件

- 凭证缺失：停止，请用户在当前聊天中粘贴 Token，或提示部署管理员配置 `ESIGN_TOKEN`。
- 凭证失效：停止，保留腾讯错误码，请用户获取新 Token；不自动重试旧 Token。
- 文件不是 PDF、文件不存在、过大或路径不可访问：停止并给出可执行的文件准备建议。
- 网络、额度或腾讯服务错误：完整保留错误码，隐藏凭证，不自动连续重试。用户明确要求后最多重试一次。
- 上传成功但生成链接失败：说明文件已发送至腾讯电子签但未生成入口；从错误详情保留 `resource_id`，用户提供有效 Token 后只重试 `prepare`，不要重复上传。
- 用户撤回授权、文件版本变化或出现新风险：立即停止，重新从文件确认和风险门开始。

## 安全说明

电子签属于外部数据传输和高影响业务操作。即使审批意见为“同意”，也不能自动触发上传；即使已生成链接，也不能声称合同已经签署。聊天 Token 会进入当前会话和工具调用参数，但脚本不得把它写入文件、响应正文或额外日志。生产环境仍应由部署方控制腾讯电子签账户权限、额度和 Token 轮换，并遵守腾讯电子签的服务协议与隐私政策。
