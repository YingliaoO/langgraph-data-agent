# 第三方组件与数据归属

- Neo4j 的 APOC 插件通过官方镜像的 `NEO4J_PLUGINS` 配置在运行时加载；本公开快照不再分发历史来源待核验的 JAR。APOC Core 上游采用 Apache License 2.0，版权与第三方依赖见 [Neo4j APOC](https://github.com/neo4j/apoc) 和 [APOC third-party licenses](https://neo4j.com/terms/thirdpartylicenses/apoc/)。Neo4j 镜像及插件遵循各自许可条款。
- Python 和 npm 依赖由各自发布者授权，后端依赖声明见 `pyproject.toml`，知识服务依赖见 `kb_ingest/requirements.txt`，前端依赖版本见 `web/package-lock.json`。项目根目录的 Apache 2.0 许可证不替代这些依赖的许可证。
- `demo/enterprise_retail/` 是项目的合成演示数据。历史 Recipe 数据、词典、表格和旧评测集的再分发权尚未确认，因此未纳入本公开快照。
