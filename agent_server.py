import asyncio
import sys

from mcp.server.fastmcp import FastMCP

from browser import account_session
from publisher import acknowledge_verification, add_account, batch_status, cancel_verification as request_cancel_verification, list_accounts, prepare_batch, run_batch
import inbox


server = FastMCP("OwnPublisher")


@server.tool()
def inbox_accounts() -> dict[str, object]:
    """列出私信账号的本地同步状态；不发送任何消息。"""
    return inbox.inbox_accounts()


@server.tool()
def inbox_conversations(alias: str = '', query: str = '', status: str = 'all',
                        limit: int = 100, offset: int = 0) -> dict[str, object]:
    """查询本机已同步的私信会话，可按账号、关键词、new、pending、processed 或 important 筛选。"""
    return inbox.list_conversations(alias=alias, query=query, status=status, limit=limit, offset=offset)


@server.tool()
def inbox_messages(conversation_id: int | None = None, limit: int = 200, offset: int = 0,
                   conversation_key: str = '') -> dict[str, object]:
    """按旧版数字 ID 或稳定 conversation_key 读取本机消息；注意完整性标记。"""
    if conversation_key:
        resolved = inbox.conversation_id_for_key(conversation_key)
        if conversation_id is not None and conversation_id != resolved:
            raise ValueError('会话标识不一致。')
        conversation_id = resolved
    if conversation_id is None:
        raise ValueError('请提供会话标识。')
    return inbox.conversation(conversation_id, limit=limit, offset=offset)


@server.tool()
def accounts() -> dict[str, list[dict]]:
    """列出本机登记的小红书和抖音账号别名。"""
    return {"accounts": list_accounts()}


@server.tool()
def register_account(alias: str, platform: str, label: str = "") -> dict[str, str]:
    """登记账号。platform 为 xiaohongshu 或 douyin。首次登录使用本地 CLI。"""
    return add_account(alias, platform, label)


@server.tool()
def check_account(alias: str) -> dict[str, str]:
    """打开该账号的独立会话检查是否能进入创作者图文页。"""
    return account_session(alias)


@server.tool()
def preview_batch(manifest_path: str) -> dict[str, object]:
    """预览各账号素材、文案和上次核验昵称。identity_verified 为 false 时先检查账号并重新预览。"""
    return prepare_batch(manifest_path)


@server.tool()
async def publish_batch(batch_id: str, confirmation: str, retry_failed: bool = False,
                        interactive: bool = False, allow_duplicate_accounts: list[str] | None = None) -> dict[str, object]:
    """向真实平台发布。用户须明确授权批次；相同图文再次发布须另获确认并传对应账号。"""
    def verification(alias, detail):
        if detail:
            print(f"{alias}：{detail} 完成后可在工作台继续，或调用 continue_verification。", file=sys.stderr, flush=True)
    options = {"retry": retry_failed, "on_verification": verification if interactive else None}
    if allow_duplicate_accounts is not None:
        options["allow_duplicate_accounts"] = allow_duplicate_accounts
    return await asyncio.to_thread(run_batch, batch_id, confirmation, **options)


@server.tool()
def continue_verification(batch_id: str) -> dict[str, bool]:
    """用户完成验证后继续检查；仅当平台明确拒绝上次请求并要求验证时，最多再提交一次。"""
    return acknowledge_verification(batch_id)


@server.tool()
def cancel_verification(batch_id: str) -> dict[str, bool]:
    """取消等待验证；若已尝试提交，结果会标记为待人工核对。"""
    return request_cancel_verification(batch_id)


@server.tool()
def publication_status(batch_id: str) -> dict[str, object]:
    """读取逐账号提交记录。submitted 表示页面确认提交，uncertain 需要到平台核对。"""
    return batch_status(batch_id)


if __name__ == "__main__":
    server.run(transport="stdio")
