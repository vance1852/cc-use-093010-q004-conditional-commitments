# 全球数字贸易合作运营平台

本项目是一套可离线运行的 Python 服务端平台，用于管理跨境数字合作中的资源流转、项目证据评估和统计资料质量。平台把运营节点、合作通道、资源申请、项目版本、分析决定、样本观测、权限和审计事件持久化到 SQLite，供秘书处、项目办公室、数据团队和审计人员协作使用。

## 目录

- src/trade_flow/：运营节点、合作通道、资源批次、额度申请、分配和情景分析；
- src/cooperation_assurance/：合作项目、证据版本、评估协议、观测导入、分析任务与准入决定；
- src/commitment_control/：条件化承诺版本、前置证据与依赖、占用/生效/释放/回收台账、释放要约与稳定候补转配、里程碑阻断解释；
- src/metric_quality/：统计样本批次、指标观测、质量分析、账号权限和审批；
- fixtures/：离线验收使用的评估协议与结构化观测；
- tests/：领域规则、错误边界、事务、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m trade_flow.acceptance --workspace .
    PYTHONPATH=src python3 -m cooperation_assurance.acceptance --workspace .
    PYTHONPATH=src python3 -m commitment_control.acceptance --workspace .
    PYTHONPATH=src python3 -m metric_quality.acceptance

四条命令会在临时 SQLite 数据库中完成合作资源流转、项目证据评估、条件化承诺（部分生效、失败释放、候补转配、里程碑阻断）和统计资料质量流程，不访问外部网络。

## HTTP 服务

    PYTHONPATH=src python3 -m trade_flow.api --database trade-flow.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m cooperation_assurance.api --database cooperation-assurance.sqlite3 --host 127.0.0.1 --port 8081
    PYTHONPATH=src python3 -m metric_quality.api --database metric-quality.sqlite3 --host 127.0.0.1 --port 8082
    PYTHONPATH=src python3 -m commitment_control.api --database commitment-control.sqlite3 --host 127.0.0.1 --port 8083

服务提供 JSON 接口与健康检查。进程重启后可以继续读取 SQLite 中的业务状态和审计历史。

## 条件化承诺管理

承诺按版本冻结每项资源的提供方、受益方、前置证据、条件相互依赖、释放顺序、有效期和退出责任
（退回提供方 `return_provider` 或转配候补 `reallocate_pool`）：

- 条件独立满足即可部分生效；条件失败沿依赖链级联，只释放仍未兑现的部分；
- 释放出的未兑现额度形成**释放要约**（池余额中占用转为要约预留，新的直接承诺不能挪用），
  候补按 `(优先级, 登记时间, 分片编号)` 稳定排序竞争，一次竞争至多一个胜者，
  可逐要约追溯被保留资源最终去了哪里；逾期未消费的要约退回池；
- 已核验通过的交付只追加、不可改删，后续版本和规则不能重排；
- 通知以稳定键幂等；里程碑给出逐条阻断原因；
  任一资源可经 `GET /resources/trace/{id}` 追到承诺版本、证据、占用、生效、交付、释放、要约与回收全过程。
