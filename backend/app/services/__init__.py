"""服务层（FUNCTION-MAP §0 第 3 条：每个模块服务放在 `backend/app/services/<module>.py`）。

分层纪律（容易违反、违反后很难发现）：

- **`engines/` 是纯算法**：不读库、不调模型、不碰网络。判定逻辑必须能在没有 MySQL、
  没有模型的情况下离线复算，否则"结果可复现"就无从谈起。
- **`services/` 持 IO**：读库、写库、调用 Provider，并把结果装配成引擎需要的纯输入。
- **`api/routes/` 只做 DTO 校验 → H01 当前身份 → H02 功能检查 → 服务 → 安全响应**（FUNCTION-MAP §0 第 4 条），
  不在路由里写业务判断。

当前已落地：

- `authz_svc`：H04 `authorize_units` 的读库与装配（纯判定仍由 `engines.permission` 承担）；
- `auth_svc`：F-01.02 刷新令牌的持久撤销/消费仓储。
"""
