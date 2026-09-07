"""测试全局夹具：单元/集成测试一律离线运行。

进程内禁用 TMDB（无论配置里有没有 api_key 都不查网）：
- 测试结果不随真实 TMDB 数据漂移（同名条目、首播年份、地区都可能变化，
  例如「夏季」在 TMDB 上是 2026 年同名韩剧，会带偏地区判定）；
- 不依赖外网，CI / 离线环境可跑。

需要覆盖「TMDB 在环」行为的用例，请自行 monkeypatch
core.media_meta._tmdb_key / _tmdb_search。
"""
import core.media_meta as _mm

# 直接占住进程内缓存：_tmdb_key() 见到非 None 就短路，不再读配置/发请求
_mm._tmdb_key_cache = ""
_mm._tmdb_proxy_cache = ""
