# 绿色金融能力凭证核验引擎

项目用于管理能力目录、机构凭证和岗位核验，并承接年度续期学分审议。Litestar 提供异步 HTTP 运行环境，SQLAlchemy 与 Alembic 只访问 SQLite，默认数据库位于 `data/skills.sqlite3`。

```bash
python -m pip install -r requirements.txt
python -m alembic upgrade head
pytest
uvicorn skill_engine:app
```

数据库位置由 `DATABASE_PATH` 覆盖，服务端口通过启动参数或 `PORT` 传入。源码、迁移与测试相互分离，容器启动前会先执行结构升级。

## 续期审议能力

- **课程目录**：课程保存能力范围、有效期、地区适用、等效组；周期上限按能力范围或等效组配置（`/admin/period-caps`）；等效规则按版本发布（`/admin/equivalence-versions`），换版只影响尚未生效的申请。
- **证明受理**：提供方上传完成证明（`/proofs`）带内容摘要。相同证明再次提交沿用原记录；同标识异文暂停计入并进入核查队列（`/verification-cases`），核查结论决定恢复计入或作废。
- **学分计算**：发起申请（`/applications`）时在滚动周期内逐项评估，结论分为 counted / capped / excluded，逐项说明重复、封顶、过期、范围不符、地区不适配、提供方撤销、证明作废或待核查，并固化快照。
- **审议**：直属审核人（supervisor）看专业范围，独立合规人（compliance）看利益冲突；审核人不能是申请人，也不能与课程提供方存在受限关系（`/admin/reviewers/{id}/restrictions`）。意见只能落在审核人看到的快照版本上，双方批准后续期生效。
- **触发重评**：提供方撤销、证明作废、等效换版只重新评估尚未生效的申请；已生效续期保留原证据，追加风险标注（`/me/renewals` 可见）。
- **查询**：申请人经 `/me/applications` 查看自己的逐项明细与审议事件；对外接口 `/external/credentials/{code}/validity?on=YYYY-MM-DD` 只回答指定日期是否有效。
- **抗中断**：核查队列、审议期限与重评工作全部落在持久作业表，后台 worker 周期执行，进程重启后继续处理；`/ops/jobs/tick` 可手动驱动一轮（测试与运维用）。

## 开发检查

- 编译检查：`python3 -m compileall -q src`
- 测试时钟：设置 `SKILL_ENGINE_NOW`（ISO 时间）可固定"当前时间"，便于演练期限与滚动窗口。
