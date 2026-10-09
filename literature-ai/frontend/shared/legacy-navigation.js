/* Compatibility for saved URLs. Only navigates; never starts extraction or writes data. */
(function () {
  "use strict";
  const script = document.currentScript;
  const target = new URL(script.dataset.target, location.origin);
  const params = new URLSearchParams(location.search);
  if (params.has("id") && !params.has("paper_id")) params.set("paper_id", params.get("id"));
  if (script.dataset.type) params.set("data_type", script.dataset.type);
  if (target.pathname.includes("paper_detail") && !params.get("paper_id")) target.pathname = "/pages/literature_library/index.html";
  target.search = params.toString();
  target.hash = location.hash;
  location.replace(target.pathname + target.search + target.hash);
})();
