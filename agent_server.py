from mcp.server.fastmcp import FastMCP

from browser import account_session
from publisher import add_account, batch_status, list_accounts, prepare_batch, run_batch


server = FastMCP("OwnPublisher")


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
    """读取本地图文 JSON，展示每个账号将发布的素材和文案，并返回批次号。"""
    return prepare_batch(manifest_path)


@server.tool()
def publish_batch(batch_id: str, confirmation: str, retry_failed: bool = False) -> dict[str, object]:
    """向真实平台发布。仅在用户明确授权该批次内容及目标后调用，confirmation 传已确认批次号。"""
    return run_batch(batch_id, confirmation, retry=retry_failed)


@server.tool()
def publication_status(batch_id: str) -> dict[str, object]:
    """读取逐账号提交记录。submitted 表示页面确认提交，uncertain 需要到平台核对。"""
    return batch_status(batch_id)


if __name__ == "__main__":
    server.run(transport="stdio")
