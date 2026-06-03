loadLibraries = async function() {
    try {
        const libraries = normalizeLibraryListResponse(await fetchJSON(LIB_API));
        const el = $("librarySelect");
        const active = (libraries || []).find(function(item) { return item.is_active; }) || null;
        const fallback = (libraries && libraries.length) ? libraries[0] : null;
        const selected = active || fallback;

        if (el) {
            el.innerHTML = (libraries || []).map(function(item) {
                const selectedAttr = selected && item.name === selected.name ? " selected" : "";
                const count = Number(item.paper_count || 0);
                return '<option value="' + esc(item.name) + '"' + selectedAttr + ">" +
                    esc(item.name) + "（" + count + " 篇）" +
                "</option>";
            }).join("");
        }

        state.currentLibrary = selected;
        state.currentLibraryTotal = selected ? Number(selected.paper_count || 0) : 0;

        const status = $("libStatus");
        if (status) {
            status.textContent = selected
                ? ((selected.root_path || selected.name) + " | " + state.currentLibraryTotal + " 篇文献")
                : "";
        }

        loadLibraryRuntimeInfo();
    } catch (error) {
        console.error("loadLibraries failed", error);
    }
};

activateLibraryByName = async function(name) {
    if (!name) return;
    try {
        await fetchJSON(LIB_API + "/" + encodeURIComponent(name) + "/activate", { method: "POST" });
        state.currentOffset = 0;
        await loadLibraries();
        refreshCurrentPage();
        showToast("已切换到：" + name, "success");
    } catch (error) {
        showToast("切库失败：" + error.message, "error");
    }
};
