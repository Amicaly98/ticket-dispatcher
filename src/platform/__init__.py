"""平台特定的 API 扩展。

每个模块通过 register_api_router() 注册自己的 FastAPI 路由，
在 app 启动时自动挂载。

当前内置：
  - bilibili: 扫码登录、活动查询、购票人列表、账号刷新

Driver 作者可以在此目录下添加自己的平台模块，参考 bilibili.py 的实现。
"""
