# Corpus Gate 技术资料收集

这是一个可独立运行的本地业务项目。核心模块保留数据接收、领域规则、持久化记录及查询接口；演练数据均为合成资料，不需要真实账号或外部业务服务。

## 运行环境

Python 3.11 或更新版本，核心业务使用标准库与 SQLite。

Dockerfile 固定运行时版本。首次准备执行 docker build -t local-product .，之后以 docker run --rm --network none local-product 运行完整检查；所有 HTTP 演练只在容器内部回环地址通信。

## 开发与运行

业务测试：
    python -m unittest discover -s tests -v
编译或语法检查：
    python -m compileall -q corpus_gate demo_support
现有业务演示：
    python -m corpus_gate --db :memory: demo --fixture fixtures/demo.json
输入端演练检查（含邮箱连接器端到端演练）：
    python demo_support/check.py
合并运行：
    sh demo_support/verify.sh

输入协议、样本与故障开关见 demo_support/协议.md。输入端检查只证明本地来源的约定行为；业务演示展示当前已有功能。调用者可以在独立临时数据目录重复运行，原始样本不会被覆盖。

## 邮箱连接器

corpus_gate/mail 把共享邮箱的 EML 投递接入既有文档流程，邮件投递证据与文档身份分表留痕（mail_syncs、mail_deliveries、mail_parts、mail_message_index）。同步与重试既可通过服务操作（mail.sync、mail.retry、mail.trace 等）调用，也可使用命令行：

    python -m corpus_gate --db corpus.sqlite3 mail-sync --mailbox <id> --base-url <来源地址> \
        --source <来源> --profile <档案> --context <授权上下文.json> [--epoch 2]
    python -m corpus_gate --db corpus.sqlite3 mail-deliveries [--state partial]
    python -m corpus_gate --db corpus.sqlite3 mail-trace <document_id>
    python -m corpus_gate --db corpus.sqlite3 mail-conflicts
    python -m corpus_gate --db corpus.sqlite3 mail-retry <delivery_id> --context <上下文.json> \
        --corrections <MIME路径到更正文本.json>

关键约定：去重身份为 mailbox + epoch + UID，Message-ID 与主题不作去重键，同号异文在 mail_message_index 标记 conflict；换 epoch 后 UID 重用产生独立投递。multipart/alternative 优先纯文本、退回 HTML 转文本；字符集与传输编码严格按 MIME 声明处理；图片与非允许类型只登记忽略原因；坏附件不影响同封正文，投递记为 partial 并可对失败部件重试。正文与附件走既有文档去重与审查，转发重复不产生多份资料，且每份文档都可反查到来源邮件的具体 MIME 部件。
