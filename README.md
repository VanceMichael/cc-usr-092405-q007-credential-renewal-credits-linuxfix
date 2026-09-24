# 绿色金融能力凭证核验引擎

服务管理**年度续期学分审议**：课程目录（能力范围、有效期、地区适用、等效组、周期上限）、提供方完成证明（内容摘要、幂等登记、同标识异文核查）、滚动周期逐项裁决、申请凭证快照、直属审核人 + 独立合规人双岗审议，以及提供方撤销/证明作废/等效换版后的重评与生效后风险留存。

Litestar 提供 HTTP 运行环境，SQLAlchemy + Alembic 只访问 SQLite，默认数据库位于 `data/skills.sqlite3`（文件库自动启用 WAL 与 busy_timeout）。

```bash
python -m pip install -r requirements.txt
python -m alembic upgrade head
pytest
uvicorn skill_engine:app
```

数据库位置由 `DATABASE_PATH` 覆盖。设置 `ENABLE_BACKGROUND_WORKER=0` 可关闭后台恢复线程（测试与手工驱动队列时使用）。

## 审议规则

### 课程目录（不可变版本）
每次发布课程产生一个新版本，保存能力范围、学分、有效期（`valid_from`/`valid_until`）与适用地区（空列表表示全境）。等效规则按组发布并可**换版**：组成员为具体的课程目录版本；同一等效组在一个滚动周期内只计一次。续期阈值按能力配置：要求学分、周期年数与周期上限（可部分计分）。

### 完成证明
- 上传带内容摘要；服务端对规范化内容（人、课、日期、学分、摘要，刻意**不含提供方**）计算指纹。
- **相同证明再次提交沿用原记录**：同提供方纯幂等返回；多机构重复提交建立指向原记录的重复行，周期裁决中说明“沿用原记录”。
- **同标识异文**（同一 `evidence_uid` 内容不同）暂停计入、进入持久化核查队列（默认 72 小时审议期限），确认后恢复、驳回则作废并触发重评。

### 滚动周期逐项裁决
每条证明给出 `included/excluded` 与中文原因，类别为：
`duplicate`（内容指纹 / 同课程 / 等效组，含多机构）、`capped`（周期上限，含部分计入）、`expired`（窗口外或课程有效期届满）、`out_of_scope`（能力范围 / 地区 / 不在目录）、`under_review`、`void`、`provider_revoked`。

### 申请快照与双岗审核
- 申请发起即固定引用的证明、课程目录版本、等效规则版本、阈值与逐项学分快照；之后的目录新增不影响在审申请。
- 直属审核人负责专业范围，独立合规人检查利益冲突；两人均不能是申请人，不能相互同人，且不得与申请引用的任一提供方存在有效受限关系。
- 意见带申请版本号：`expected_version` 与当前版本不一致即报版本冲突，并发意见只能落在各自看到的版本上；旧版本在产生新版本时标记 `obsolete`。同一版本双批准且学分达标才生效。

### 重评与生效后风险
提供方资格撤销、证明作废、等效规则换版只**重新评估尚未生效（pending）的申请**（持久化重评队列，崩溃后按僵死租约恢复；裁决无实质变化不前进版本）；**已生效续期保留原证据与原决定**，仅追加风险标注。

### 查询
- 申请人可查询自己的全部证明与逐项采用/排除明细。
- `GET /external/validity` 对外只回答 `{valid, as_of, competence}`：指定日期凭证是否有效，不暴露审议细节与风险。
- 学分溯源 `trace` 展示任一证明在各申请版本中被采用或排除的来龙去脉（指纹、目录版本、等效规则、提供方状态）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/persons` `/admin/providers` | 人员、提供方登记 |
| POST/DELETE | `/admin/provider-relationships` | 受限关系维护（合规回避依据） |
| POST | `/admin/providers/{id}/revoke` | 提供方资格撤销（分流重评/风险） |
| POST | `/admin/courses` | 发布课程目录新版本 |
| POST | `/admin/equivalence-rules` | 发布/换版等效规则 |
| POST | `/admin/requirements` | 续期阈值（要求学分、年数、周期上限） |
| GET/POST | `/admin/review-queue[/{id}/resolve]` | 核查队列与决议 |
| POST | `/admin/evidence/{id}/void` | 证明作废 |
| POST | `/provider/evidence` | 提供方上传完成证明 |
| POST/GET | `/applications[/{id}]` | 发起申请（固定快照）/ 查看 |
| POST | `/applications/{id}/opinions` | 双岗审核意见（可带 `expected_version`） |
| GET | `/applicants/{id}/detail` | 申请人本人明细 |
| GET | `/applicants/{id}/credits/{evidence_id}/trace` | 学分溯源 |
| GET | `/external/validity?applicant_id=&competence=&date=` | 对外：指定日期是否有效 |

领域冲突返回 `409 {error:"conflict", detail}`，对象不存在返回 `404`。

## 开发检查

- 编译检查：`python3 -m compileall -q src`
- 测试：`pytest`（纯函数裁决、证明幂等与核查、快照固定、双岗回避、版本并发、三类重评分流、生效后风险、队列恢复、对外接口、溯源）
