"""
Tushare API 初始化
===================
自定义代理地址 + 新 Token，替代已过期的旧配置。

使用方法:
    from scripts.tushare_init import get_tushare_pro
    pro = get_tushare_pro()
    df = pro.index_basic(limit=5)

注意: 如果显示 Token 不对，检查是否少了这行:
    pro._DataApi__http_url = "http://121.40.135.59:8010/"
"""

import os
import tushare as ts

# ── 配置 ──────────────────────────────────────────
TUSHARE_TOKEN = "376a6f79d0b08b8e63c89ae9bcdead99093efb16f3b357a164957f1f"
CUSTOM_HTTP_URL = "http://121.40.135.59:8010/"
# ──────────────────────────────────────────────────


def get_tushare_pro(token: str = None, custom_url: str = None) -> ts.pro_api:
    """初始化并返回 tushare pro 接口。

    Args:
        token: Tushare token，默认使用配置文件中的新 Token
        custom_url: 自定义代理地址，默认使用 121.40.135.59:8010

    Returns:
        ts.pro_api 实例
    """
    pro = ts.pro_api(token or TUSHARE_TOKEN)
    pro._DataApi__http_url = custom_url or CUSTOM_HTTP_URL
    return pro


def test_connection(pro=None) -> bool:
    """快速测试 Tushare API 连通性。"""
    if pro is None:
        pro = get_tushare_pro()
    try:
        df = pro.index_basic(limit=3)
        print(f"✅ Tushare 连接成功, 返回 {len(df)} 条记录")
        print(df.to_string())
        return True
    except Exception as e:
        print(f"❌ Tushare 连接失败: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    test_connection()
