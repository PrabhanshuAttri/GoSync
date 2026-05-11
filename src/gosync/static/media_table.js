(function () {
  const settingsKey = "gosync-media-table-settings";
  const extensionFilter = document.getElementById("extension-filter");
  const selectVisible = document.getElementById("select-visible");
  const selectionSummary = document.getElementById("selection-summary");
  const filesPerBatchInput = document.getElementById("files-per-batch");
  const sortButtons = Array.from(document.querySelectorAll(".sort-button"));
  const tableBody = document.getElementById("sidecar-table-body");
  const tableWrap = tableBody?.closest(".table-wrap");

  if (!extensionFilter || !selectVisible || !selectionSummary || !tableBody) {
    return;
  }

  let sidecarItems = [];
  let lastItemsSignature = "";
  let requestedExtensionFilter = restoredSettings.extensionFilter || "";
  let pendingScrollTop = Number(restoredSettings.tableScrollTop) || 0;
  let pendingScrollLeft = Number(restoredSettings.tableScrollLeft) || 0;
  let scrollSaveTimer = null;

  const readSettings = () => {
    try {
      return JSON.parse(sessionStorage.getItem(settingsKey) || "{}");
    } catch {
      return {};
    }
  };

  const restoredSettings = readSettings();
  let selectedMediaKeys = Array.isArray(restoredSettings.selectedKeys)
    ? new Set(restoredSettings.selectedKeys)
    : null;
  let mediaSort = {
    key: ["size", "status"].includes(restoredSettings.sortKey)
      ? restoredSettings.sortKey
      : "status",
    direction: ["asc", "desc"].includes(restoredSettings.sortDirection)
      ? restoredSettings.sortDirection
      : "asc",
  };

  if (filesPerBatchInput && restoredSettings.filesPerBatch) {
    filesPerBatchInput.value = restoredSettings.filesPerBatch;
  }

  const saveSettings = () => {
    sessionStorage.setItem(
      settingsKey,
      JSON.stringify({
        selectedKeys: selectedMediaKeys ? Array.from(selectedMediaKeys) : null,
        sortKey: mediaSort.key,
        sortDirection: mediaSort.direction,
        extensionFilter: extensionFilter.value,
        filesPerBatch: filesPerBatchInput?.value || "",
        tableScrollTop: tableWrap?.scrollTop || 0,
        tableScrollLeft: tableWrap?.scrollLeft || 0,
      })
    );
  };

  const itemsSignature = (items) => JSON.stringify(
    (items || []).map((item) => [
      item.key || "",
      item.filename || "",
      item.sidecar_filename || "",
      item.file_size ?? null,
      item.status || "",
    ])
  );

  const formatBytes = (bytes) => {
    if (!bytes) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let value = bytes;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return `${value.toFixed(value >= 10 || index === 0 ? 0 : 1)} ${units[index]}`;
  };

  const mediaExtension = (filename) => {
    const base = String(filename || "").split(/[\\/]/).pop();
    const index = base.lastIndexOf(".");
    return index > -1 && index < base.length - 1
      ? base.slice(index + 1).toLowerCase()
      : "";
  };

  const syncExtensionFilterOptions = () => {
    const selected = extensionFilter.value || requestedExtensionFilter;
    const extensions = Array.from(
      new Set(sidecarItems.map((item) => mediaExtension(item.filename)).filter(Boolean))
    ).sort((a, b) => a.localeCompare(b));

    extensionFilter.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All extensions";
    extensionFilter.append(allOption);

    extensions.forEach((extension) => {
      const option = document.createElement("option");
      option.value = extension;
      option.textContent = extension.toUpperCase();
      extensionFilter.append(option);
    });

    extensionFilter.value = !selected || extensions.includes(selected) ? selected : "";
    requestedExtensionFilter = extensionFilter.value;
    extensionFilter.disabled = extensions.length < 2;
  };

  const formatFileSize = (value) => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return "Unknown";
    return formatBytes(bytes);
  };

  const statusRank = (status) => {
    if (status === "downloading") return 0;
    if (status === "downloaded") return 1;
    return 2;
  };

  const fileSizeValue = (item) => {
    const bytes = Number(item.file_size);
    return Number.isFinite(bytes) && bytes > 0 ? bytes : -1;
  };

  const compareMediaItems = (a, b) => {
    let result = 0;
    if (mediaSort.key === "size") {
      const aSize = fileSizeValue(a);
      const bSize = fileSizeValue(b);
      if (aSize < 0 && bSize >= 0) result = 1;
      else if (aSize >= 0 && bSize < 0) result = -1;
      else result = (aSize - bSize) * (mediaSort.direction === "asc" ? 1 : -1);
    } else {
      result = (
        statusRank(a.status) - statusRank(b.status)
      ) * (mediaSort.direction === "asc" ? 1 : -1);
    }
    return result || String(a.filename || "").localeCompare(String(b.filename || ""));
  };

  const syncSortButtons = () => {
    sortButtons.forEach((button) => {
      const active = button.dataset.sortKey === mediaSort.key;
      const indicator = button.querySelector(".sort-indicator");
      button.classList.toggle("active", active);
      button.setAttribute("aria-sort", active ? mediaSort.direction : "none");
      indicator?.classList.toggle("asc", active && mediaSort.direction === "asc");
      indicator?.classList.toggle("desc", active && mediaSort.direction === "desc");
    });
  };

  const visibleSelectableItems = () => {
    const selectedExtension = extensionFilter.value;
    return sidecarItems.filter((item) => {
      const visible = selectedExtension
        ? mediaExtension(item.filename) === selectedExtension
        : true;
      return visible && item.status !== "downloaded" && item.key;
    });
  };

  const updateSelectionSummary = () => {
    const selectedCount = selectedMediaKeys ? selectedMediaKeys.size : 0;
    const pendingCount = sidecarItems.filter(
      (item) => item.status !== "downloaded"
    ).length;
    selectionSummary.textContent = `${selectedCount} of ${pendingCount} pending selected`;
  };

  const syncSelectVisibleState = () => {
    const visible = visibleSelectableItems();
    const selectedVisible = visible.filter((item) => selectedMediaKeys?.has(item.key));
    selectVisible.disabled = !visible.length;
    selectVisible.checked = !!visible.length && selectedVisible.length === visible.length;
    selectVisible.indeterminate = (
      selectedVisible.length > 0 && selectedVisible.length < visible.length
    );
  };

  const render = () => {
    const previousScrollTop = tableWrap?.scrollTop || pendingScrollTop;
    const previousScrollLeft = tableWrap?.scrollLeft || pendingScrollLeft;
    const selectedExtension = extensionFilter.value;
    const items = selectedExtension
      ? sidecarItems.filter((item) => mediaExtension(item.filename) === selectedExtension)
      : sidecarItems;
    const sortedItems = [...items].sort(compareMediaItems);

    if (!sidecarItems.length) {
      tableBody.innerHTML = '<tr><td colspan="5" class="empty-cell">No media found.</td></tr>';
      return;
    }
    if (!items.length) {
      tableBody.innerHTML = '<tr><td colspan="5" class="empty-cell">No media matches this filter.</td></tr>';
      return;
    }

    tableBody.replaceChildren();
    sortedItems.forEach((item) => {
      const row = document.createElement("tr");
      const selectCell = document.createElement("td");
      const checkbox = document.createElement("input");
      const filename = document.createElement("td");
      const sidecar = document.createElement("td");
      const size = document.createElement("td");
      const statusCell = document.createElement("td");
      const status = document.createElement("span");
      const selectable = item.status !== "downloaded";

      checkbox.type = "checkbox";
      checkbox.name = "selected_media_keys";
      checkbox.value = item.key || "";
      checkbox.checked = selectedMediaKeys?.has(item.key) || false;
      checkbox.disabled = !selectable || !item.key;
      checkbox.setAttribute("form", "start-form");
      checkbox.setAttribute("aria-label", `Select ${item.filename}`);
      checkbox.addEventListener("change", () => {
        if (!selectedMediaKeys) selectedMediaKeys = new Set();
        if (checkbox.checked) {
          selectedMediaKeys.add(item.key);
        } else {
          selectedMediaKeys.delete(item.key);
        }
        updateSelectionSummary();
        syncSelectVisibleState();
        saveSettings();
      });

      filename.textContent = item.filename;
      filename.title = item.filename;
      sidecar.textContent = item.sidecar_filename;
      sidecar.title = item.sidecar_filename;
      size.textContent = formatFileSize(item.file_size);
      size.title = item.file_size ? `${item.file_size} bytes` : "Unknown size";
      status.className = `table-status ${item.status}`;
      status.textContent = item.status;

      selectCell.append(checkbox);
      statusCell.append(status);
      row.append(selectCell, filename, sidecar, size, statusCell);
      tableBody.append(row);
    });
    if (tableWrap) {
      tableWrap.scrollTop = previousScrollTop;
      tableWrap.scrollLeft = previousScrollLeft;
      pendingScrollTop = 0;
      pendingScrollLeft = 0;
    }
    syncSelectVisibleState();
    updateSelectionSummary();
  };

  tableWrap?.addEventListener("scroll", () => {
    window.clearTimeout(scrollSaveTimer);
    scrollSaveTimer = window.setTimeout(saveSettings, 150);
  });

  sortButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const key = button.dataset.sortKey;
      if (mediaSort.key === key) {
        mediaSort.direction = mediaSort.direction === "asc" ? "desc" : "asc";
      } else {
        mediaSort = { key, direction: "desc" };
      }
      syncSortButtons();
      saveSettings();
      render();
    });
  });

  selectVisible.addEventListener("change", () => {
    if (!selectedMediaKeys) selectedMediaKeys = new Set();
    visibleSelectableItems().forEach((item) => {
      if (selectVisible.checked) {
        selectedMediaKeys.add(item.key);
      } else {
        selectedMediaKeys.delete(item.key);
      }
    });
    saveSettings();
    render();
  });

  filesPerBatchInput?.addEventListener("input", saveSettings);
  document.getElementById("start-form")?.addEventListener("submit", saveSettings);
  extensionFilter.addEventListener("change", () => {
    requestedExtensionFilter = extensionFilter.value;
    saveSettings();
    render();
  });

  window.gosyncMediaTable = {
    setItems(items) {
      const nextSignature = itemsSignature(items);
      if (nextSignature === lastItemsSignature) {
        return;
      }
      lastItemsSignature = nextSignature;
      sidecarItems = items || [];
      if (selectedMediaKeys === null) {
        selectedMediaKeys = new Set(
          sidecarItems
            .filter((item) => item.status !== "downloaded" && item.key)
            .map((item) => item.key)
        );
      } else {
        const validKeys = new Set(sidecarItems.map((item) => item.key).filter(Boolean));
        selectedMediaKeys = new Set(
          Array.from(selectedMediaKeys).filter((key) => validKeys.has(key))
        );
      }
      syncExtensionFilterOptions();
      syncSortButtons();
      render();
      saveSettings();
    },
  };

  syncSortButtons();
}());
