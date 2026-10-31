(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MLExplorerAdapter = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SCHEMA_VERSION = "ml-explorer-candidate.v2";
  const RUNTIME_SCHEMA_VERSION = "ml-explorer-runtime.v2";
  const LIBRARY_NAME = "锂硫双原子";
  const DOMAIN = "LITHIUM_SULFUR_MODEL";
  const SHAP_RUN = "shap_explanation_v2_20261031T053826Z";
  const EXPECTED_OOF = Object.freeze({
    adsorption_li2s: 81,
    adsorption_li2s2: 51,
    adsorption_li2s4: 79,
    adsorption_li2s6: 90,
    adsorption_li2s8: 63,
    adsorption_s8: 47,
    decomposition_barrier_li2s: 51,
  });
  const CURRENT_TARGETS = Object.freeze(["kinetic_activation_energy", "li2s_full_path_max_barrier", "li2s_step1_local_barrier", "li2s_step2_local_barrier", "rds_delta_g_numeric", "rds_step_category"]);
  const HISTORICAL_BARRIER_DEFINITIONS = Object.freeze(["LI2S_DECOMPOSITION_BARRIER", "LI2S_DISSOCIATION_BARRIER"]);
  const HISTORICAL_RDS_DEFINITIONS = Object.freeze(["HISTORICAL_RDS_BARRIER", "HISTORICAL_RDS_FREE_ENERGY", "delta_g_rds", "rate_determining_step_gibbs_free_energy", "rds_free_energy_change", "rds_gibbs_energy", "rds_gibbs_free_energy"]);
  const MATERIAL_SCOPES = Object.freeze(["audit_pool_720", "model_411"]);
  const SHAP_TARGETS = Object.freeze([
    "adsorption_li2s", "adsorption_li2s2", "adsorption_li2s4",
    "adsorption_li2s6", "adsorption_li2s8", "adsorption_s8",
  ]);

  function invariant(condition, message) {
    if (!condition) throw new Error(message);
  }

  function sorted(value) {
    if (Array.isArray(value)) return value.map(sorted);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sorted(value[key])]));
    }
    return value;
  }

  function canonicalJson(value) {
    return JSON.stringify(sorted(value));
  }

  async function sha256Hex(value) {
    const bytes = typeof value === "string" ? new TextEncoder().encode(value) : new Uint8Array(value);
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function sameSet(actual, expected) {
    const left = Array.from(new Set(actual)).sort();
    const right = Array.from(new Set(expected)).sort();
    return JSON.stringify(left) === JSON.stringify(right);
  }

  async function validatePayload(runtimeManifest, payload) {
    invariant(runtimeManifest && runtimeManifest.schemaVersion === RUNTIME_SCHEMA_VERSION, "运行时 manifest schema 不匹配");
    invariant(runtimeManifest.adapterEntry === "data/app_data.json", "运行时只允许固定 app_data 入口");
    invariant(runtimeManifest.integrityAuthority === "client_best_effort_integrity_not_access_control", "完整性校验边界标识缺失");
    invariant(payload && payload.schemaVersion === SCHEMA_VERSION, "数据包 schema 版本不匹配");
    invariant(payload.manifest && payload.manifest.libraryName === LIBRARY_NAME, "数据包未绑定锂硫双原子单库");
    invariant(payload.manifest.accessScope === "isolated_candidate_preview_only", "数据包不是隔离候选范围");
    invariant(payload.manifest.integrityAuthority === "client_bundle_hash_and_allowlist_are_integrity_checks_not_access_control", "payload 完整性边界标识缺失");
    invariant(runtimeManifest.scope.libraryName === LIBRARY_NAME, "运行时 manifest 库名错误");
    invariant(sameSet(runtimeManifest.scope.domains, [DOMAIN]), "运行时 domain 白名单必须仅含锂硫模型域");
    invariant(sameSet(payload.manifest.dataDomains, [DOMAIN]), "payload domain 白名单错误");
    invariant(runtimeManifest.scope.unitCount === 177948, "单库统计单元必须为 177948");
    invariant(runtimeManifest.scope.exactMaterialContexts === 5775, "单库精确材料上下文必须为 5775");
    invariant(payload.manifest.inputHashesDigest === runtimeManifest.inputHashesDigest, "输入 hash digest 不一致");
    invariant(canonicalJson(payload.manifest.allow) === canonicalJson(runtimeManifest.allow), "payload 与运行时 allowlist 不一致");
    invariant(await sha256Hex(canonicalJson(payload.manifest)) === runtimeManifest.payloadManifestDigest, "payload manifest 被篡改");
    invariant(await sha256Hex(canonicalJson(payload.manifest.inputHashes)) === runtimeManifest.inputHashesDigest, "冻结输入 hash 清单被篡改");
    validateData(payload, runtimeManifest.allow);
    return payload;
  }

  function validateData(payload, allow) {
    const domain = payload.overview.library;
    invariant(domain.id === DOMAIN && domain.uniqueValues === 177948 && domain.rows === 22678 && domain.exactMaterials === 5775, "单库总账计数错误");
    invariant(domain.roles.length === 8 && domain.roles.reduce((sum, role) => sum + role.uniqueValues, 0) === 177948, "单库用途桶必须八项且合计一致");
    invariant(sameSet(allow.domains, [DOMAIN]), "allow.domain 错误");

    const oofTargets = payload.targets.oof;
    invariant(sameSet(allow.oofTargetIds, Object.keys(EXPECTED_OOF)), "OOF allowlist 不在固定白名单");
    invariant(sameSet(oofTargets.map((target) => target.sourceKey), allow.oofTargetIds), "OOF target 白名单不匹配");
    for (const target of oofTargets) {
      invariant(target.n === EXPECTED_OOF[target.sourceKey], `OOF 计数错误：${target.sourceKey}`);
      invariant(target.model === "dummy_median", `OOF 模型白名单拒绝：${target.model}`);
      invariant(target.targetSpecificModelAvailable === false, "历史 dummy OOF 不得宣称目标模型");
      invariant(target.points.every((point) => point[9] === DOMAIN), "OOF 点越出单库 domain");
    }
    const decomposition = oofTargets.find((target) => target.sourceKey === "decomposition_barrier_li2s");
    invariant(decomposition.historicalSourceRows === 58 && decomposition.excludedOtherDomainRows === 7, "OOF 58 历史域拆分错误");

    const readyTargets = payload.targets.readiness.targets;
    invariant(sameSet(allow.currentTargetIds, CURRENT_TARGETS), "当前 readiness allowlist 不在固定白名单");
    invariant(sameSet(readyTargets.map((target) => target.sourceKey), allow.currentTargetIds), "当前 readiness target 白名单不匹配");
    const ready = Object.fromEntries(readyTargets.map((target) => [target.sourceKey, target]));
    invariant(ready.rds_delta_g_numeric.n === 68, "当前 RDS 宽集必须为 68");
    invariant(ready.rds_delta_g_numeric.conditionComparableCount === 64, "当前 RDS 部分可比必须为 64");
    invariant(ready.rds_delta_g_numeric.strictConditionCount === 0, "当前 RDS 严格层必须为 0");
    invariant(ready.li2s_step1_local_barrier.upstreamRows === 43 && ready.li2s_step1_local_barrier.n === 35 && ready.li2s_step1_local_barrier.excludedOtherDomainRows === 8, "Step I 范围拆分错误");
    invariant(ready.li2s_step2_local_barrier.n === 0 && ready.li2s_full_path_max_barrier.n === 0, "Step II/full path 必须为零样本");
    invariant(ready.kinetic_activation_energy.n === 260 && ready.kinetic_activation_energy.upstreamRows === 271, "动力学目标范围错误");
    invariant(readyTargets.every((target) => target.modelAvailable === false && target.shapAvailable === false), "当前 readiness 不得宣称模型或 SHAP");
    invariant(readyTargets.every((target) => !target.points || target.points.every((point) => point.dataDomain === DOMAIN)), "readiness 点越出单库 domain");

    const historicalBarrier = payload.targets.historicalBarrierDefinitions;
    const historicalRds = payload.targets.historicalRdsDefinitions;
    invariant(sameSet(allow.historicalBarrierDefinitionIds, HISTORICAL_BARRIER_DEFINITIONS), "历史能垒 allowlist 不在固定白名单");
    invariant(sameSet(allow.historicalRdsDefinitionIds, HISTORICAL_RDS_DEFINITIONS), "历史 RDS allowlist 不在固定白名单");
    invariant(sameSet(historicalBarrier.map((item) => item.id), allow.historicalBarrierDefinitionIds), "历史能垒定义白名单不匹配");
    invariant(sameSet(historicalRds.map((item) => item.id), allow.historicalRdsDefinitionIds), "历史 RDS 定义白名单不匹配");
    const historicalFamily = historicalRds.find((item) => item.id === "HISTORICAL_RDS_FREE_ENERGY");
    invariant(historicalFamily.n === 89 && historicalFamily.primaryCount === 72, "历史 RDS 族必须保留 89/72");
    invariant(historicalRds.find((item) => item.id === "rds_gibbs_energy").n === 3, "历史 rds_gibbs_energy 必须保留 3 条");

    invariant(sameSet(allow.materialScopeIds, MATERIAL_SCOPES), "材料 allowlist 不在固定白名单");
    invariant(sameSet(payload.materials.scopes.map((scope) => scope.id), allow.materialScopeIds), "材料范围白名单不匹配");
    invariant(payload.shap.runId === SHAP_RUN && payload.shap.scope === "material_descriptor_cv", "SHAP run/protocol 不在白名单");
    for (const featureSet of ["F1", "F2"]) {
      const view = payload.shap.views[featureSet];
      invariant(view.modelId === "random_forest", "SHAP model 不在白名单");
      invariant(view.protocol === "material_descriptor_cv", "SHAP view protocol 错误");
      invariant(view.runId === SHAP_RUN, "SHAP view run 错误");
      invariant(view.nRecords === 411 && view.features.length === 12 && view.nFeaturePoints === 4932, `SHAP 计数错误：${featureSet}`);
      invariant(sameSet(Object.keys(view.targetCounts), SHAP_TARGETS), `SHAP target 白名单不匹配：${featureSet}`);
      invariant(view.features.every((feature) => feature.points.length === 411), `SHAP 每特征必须 411 点：${featureSet}`);
      invariant(view.features.every((feature) => feature.points.every((point) => point[8] === DOMAIN)), `SHAP 点越出单库 domain：${featureSet}`);
    }
  }

  async function validateRuntime(runtimeManifest, payload, bundleBytes) {
    invariant(bundleBytes !== undefined && bundleBytes !== null, "缺少 bundle 字节");
    invariant(runtimeManifest.bundle.sha256 === await sha256Hex(bundleBytes), "app_data 字节 hash 不匹配");
    invariant(runtimeManifest.bundle.bytes === new Uint8Array(bundleBytes).byteLength, "app_data 字节数不匹配");
    invariant(runtimeManifest.bundle.payloadSchemaVersion === payload.schemaVersion, "bundle payload schema 不匹配");
    await validatePayload(runtimeManifest, payload);
    return payload;
  }

  async function load(manifestUrl = "./data/manifest.json") {
    const manifestResponse = await fetch(manifestUrl, { method: "GET", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
    if (!manifestResponse.ok) throw new Error(`运行时 manifest 读取失败（HTTP ${manifestResponse.status}）`);
    const runtimeManifest = await manifestResponse.json();
    invariant(runtimeManifest.adapterEntry === "data/app_data.json", "运行时入口被篡改");
    const bundleUrl = new URL(runtimeManifest.adapterEntry, document.baseURI);
    const response = await fetch(bundleUrl, { method: "GET", credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`数据读取失败（HTTP ${response.status}）`);
    const bytes = await response.arrayBuffer();
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    await validateRuntime(runtimeManifest, payload, bytes);
    return { payload, runtimeManifest };
  }

  async function create(payload, runtimeManifest, bundleBytes) {
    if (bundleBytes !== undefined) await validateRuntime(runtimeManifest, payload, bundleBytes);
    else await validatePayload(runtimeManifest, payload);
    const data = payload;

    function requireOof(id) {
      const target = data.targets.oof.find((item) => item.id === id);
      invariant(target, `未授权的 OOF 目标：${id}`);
      return target;
    }
    function requireCurrent(id) {
      const key = id.replace(/^readiness:/, "");
      const target = data.targets.readiness.targets.find((item) => item.sourceKey === key);
      invariant(target, `未授权的当前 readiness 目标：${id}`);
      return target;
    }
    function requireBarrier(id) {
      const target = data.targets.historicalBarrierDefinitions.find((item) => item.id === id);
      invariant(target, `未授权的历史能垒定义：${id}`);
      return target;
    }
    function requireRds(id) {
      const target = data.targets.historicalRdsDefinitions.find((item) => item.id === id);
      invariant(target, `未授权的历史 RDS 定义：${id}`);
      return target;
    }
    function requireMaterial(id) {
      const scope = data.materials.scopes.find((item) => item.id === id);
      invariant(scope, `未授权的材料范围：${id}`);
      return scope;
    }

    return Object.freeze({
      manifest: runtimeManifest,
      payloadManifest: data.manifest,
      overview: () => data.overview.library,
      oofTargets: () => data.targets.oof.slice(),
      oofTarget: requireOof,
      currentTargets: () => data.targets.readiness.targets.slice(),
      currentReadiness: () => data.targets.readiness,
      currentTarget: requireCurrent,
      historicalBarrierDefinitions: () => data.targets.historicalBarrierDefinitions.slice(),
      historicalBarrierDefinition: requireBarrier,
      historicalRdsDefinitions: () => data.targets.historicalRdsDefinitions.slice(),
      historicalRdsDefinition: requireRds,
      rdsRule: data.targets.rdsRule,
      materialScopes: () => data.materials.scopes.slice(),
      materialScope: requireMaterial,
      shap: (featureSet) => {
        const view = data.shap.views[featureSet];
        invariant(view, `未授权的 SHAP 视图：${featureSet}`);
        return view;
      },
      shapMeta: () => ({
        runId: data.shap.runId,
        scope: data.shap.scope,
        scopeLabel: data.shap.scopeLabel,
        modelId: "random_forest",
        claimBoundary: data.shap.claimBoundary,
        countRule: data.shap.countRule,
      }),
      modelSummary: () => data.modelSummary.protocols.slice(),
    });
  }

  return Object.freeze({
    load,
    create,
    validateRuntime,
    validatePayload,
    sha256Hex,
    canonicalJson,
    SCHEMA_VERSION,
    RUNTIME_SCHEMA_VERSION,
    LIBRARY_NAME,
    EXPECTED_OOF,
  });
});
