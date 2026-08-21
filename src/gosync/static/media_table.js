(function () {
  const settingsKey = "gosync-media-table-settings";
  const extensionFilter = document.getElementById("extension-filter");
  const statusFilter = document.getElementById("status-filter");
  const selectVisible = document.getElementById("select-visible");
  const selectRemergeVisible = document.getElementById("select-remerge-visible");
  const rerunMergeCount = document.getElementById("rerun-merge-count");
  const selectionSummary = document.getElementById("selection-summary");
  const filesPerBatchInput = document.getElementById("files-per-batch");
  const sortButtons = Array.from(document.querySelectorAll(".sort-button"));
  const tableBody = document.getElementById("sidecar-table-body");
  const tableWrap = tableBody?.closest(".table-wrap");

  if (!extensionFilter || !statusFilter || !selectVisible || !selectionSummary || !tableBody) {
    return;
  }

  const readSettings = () => {
    try {
      return JSON.parse(sessionStorage.getItem(settingsKey) || "{}");
    } catch {
      return {};
    }
  };

  const restoredSettings = readSettings();
  let sidecarItems = [];
  let lastItemsSignature = "";
  let requestedExtensionFilter = restoredSettings.extensionFilter || "";
  let requestedStatusFilter = restoredSettings.statusFilter || "";
  let pendingScrollTop = Number(restoredSettings.tableScrollTop) || 0;
  let pendingScrollLeft = Number(restoredSettings.tableScrollLeft) || 0;
  let scrollSaveTimer = null;
  let selectedMediaKeys = Array.isArray(restoredSettings.selectedKeys)
    ? new Set(restoredSettings.selectedKeys)
    : null;
  let selectedMergeKeys = new Set(
    Array.isArray(restoredSettings.selectedMergeKeys) ? restoredSettings.selectedMergeKeys : []
  );
  let mediaSort = {
    key: ["size", "status", "captured_at", "item_count"].includes(restoredSettings.sortKey)
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
        selectedMergeKeys: Array.from(selectedMergeKeys),
        sortKey: mediaSort.key,
        sortDirection: mediaSort.direction,
        extensionFilter: extensionFilter.value,
        statusFilter: statusFilter.value,
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
      item.item_count ?? 1,
      item.file_size ?? null,
      item.captured_at || "",
      item.status || "",
      item.merge_status || "",
      item.remerge_eligible || false,
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
    return `${value.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
  };

  const mediaExtension = (filename) => {
    const base = String(filename || "").split(/[\\/]/).pop();
    const index = base.lastIndexOf(".");
    return index > -1 && index < base.length - 1
      ? base.slice(index + 1).toLowerCase()
      : "";
  };

  const isDownloaded = (item) => item.status === "downloaded";

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

  const statusLabels = {
    downloading: "Downloading",
    downloaded: "Downloaded",
    pending: "Pending",
  };

  const mergeStatusLabels = {
    merged: "Merged",
    size_mismatch: "Size mismatch",
    chapters_ready: "Chapters ready",
    chapters_partial: "Chapters partial",
    chapters_missing: "Chapters missing",
  };

  const syncStatusFilterOptions = () => {
    const selected = statusFilter.value || requestedStatusFilter;
    const statuses = Array.from(
      new Set(sidecarItems.map((item) => item.status).filter(Boolean))
    ).sort((a, b) => statusRank(a) - statusRank(b));

    statusFilter.replaceChildren();
    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All statuses";
    statusFilter.append(allOption);

    statuses.forEach((status) => {
      const option = document.createElement("option");
      option.value = status;
      option.textContent = statusLabels[status] || status;
      statusFilter.append(option);
    });

    statusFilter.value = !selected || statuses.includes(selected) ? selected : "";
    requestedStatusFilter = statusFilter.value;
    statusFilter.disabled = statuses.length < 2;
  };

  const formatFileSize = (value) => {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes <= 0) return "Unknown";
    return formatBytes(bytes);
  };

  const parseCapturedAt = (value) => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };

  const formatCapturedDate = (value) => {
    const date = parseCapturedAt(value);
    if (!date) return "Unknown";
    return date.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  };

  const formatCapturedDateTime = (value) => {
    const date = parseCapturedAt(value);
    if (!date) return "Unknown capture date";
    return date.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const statusRank = (status) => {
    if (status === "downloading") return 0;
    if (status === "pending") return 1;
    if (status === "downloaded") return 2;
    return 3;
  };

  const fileSizeValue = (item) => {
    const bytes = Number(item.file_size);
    return Number.isFinite(bytes) && bytes > 0 ? bytes : -1;
  };

  const capturedAtValue = (item) => {
    const time = new Date(item.captured_at || "").getTime();
    return Number.isFinite(time) && !Number.isNaN(time) ? time : -1;
  };

  const compareMediaItems = (a, b) => {
    let result = 0;
    if (mediaSort.key === "size") {
      const aSize = fileSizeValue(a);
      const bSize = fileSizeValue(b);
      if (aSize < 0 && bSize >= 0) result = 1;
      else if (aSize >= 0 && bSize < 0) result = -1;
      else result = (aSize - bSize) * (mediaSort.direction === "asc" ? 1 : -1);
    } else if (mediaSort.key === "captured_at") {
      const aTime = capturedAtValue(a);
      const bTime = capturedAtValue(b);
      if (aTime < 0 && bTime >= 0) result = 1;
      else if (aTime >= 0 && bTime < 0) result = -1;
      else result = (aTime - bTime) * (mediaSort.direction === "asc" ? 1 : -1);
    } else if (mediaSort.key === "item_count") {
      const aCount = Number(a.item_count ?? 1);
      const bCount = Number(b.item_count ?? 1);
      result = (aCount - bCount) * (mediaSort.direction === "asc" ? 1 : -1);
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

  const matchesFilters = (item) => {
    const selectedExtension = extensionFilter.value;
    const selectedStatus = statusFilter.value;
    const extensionMatches = selectedExtension
      ? mediaExtension(item.filename) === selectedExtension
      : true;
    const statusMatches = selectedStatus ? item.status === selectedStatus : true;
    return extensionMatches && statusMatches;
  };

  const visibleSelectableItems = () => (
    sidecarItems.filter((item) => (
      matchesFilters(item) && item.status !== "downloaded" && item.key
    ))
  );

  const visibleRemergeEligibleItems = () => (
    sidecarItems.filter((item) => matchesFilters(item) && item.remerge_eligible && item.key)
  );

  const updateRerunMergeSummary = () => {
    if (rerunMergeCount) {
      rerunMergeCount.textContent = String(selectedMergeKeys.size);
    }
  };

  const syncSelectRemergeVisibleState = () => {
    if (!selectRemergeVisible) return;
    const visible = visibleRemergeEligibleItems();
    const selectedVisible = visible.filter((item) => selectedMergeKeys.has(item.key));
    selectRemergeVisible.disabled = !visible.length;
    selectRemergeVisible.checked = !!visible.length && selectedVisible.length === visible.length;
    selectRemergeVisible.indeterminate = (
      selectedVisible.length > 0 && selectedVisible.length < visible.length
    );
  };

  const pendingKeys = () => (
    sidecarItems
      .filter((item) => !isDownloaded(item) && item.key)
      .map((item) => item.key)
  );

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
    const items = sidecarItems.filter(matchesFilters);
    const sortedItems = [...items].sort(compareMediaItems);

    if (!sidecarItems.length) {
      tableBody.innerHTML = '<tr><td colspan="8" class="empty-cell">No media found.</td></tr>';
      return;
    }
    if (!items.length) {
      tableBody.innerHTML = '<tr><td colspan="8" class="empty-cell">No media matches this filter.</td></tr>';
      return;
    }

    tableBody.replaceChildren();
    sortedItems.forEach((item) => {
      const row = document.createElement("tr");
      const selectCell = document.createElement("td");
      const checkbox = document.createElement("input");
      const filename = document.createElement("td");
      const size = document.createElement("td");
      const capturedAt = document.createElement("td");
      const itemCount = document.createElement("td");
      const statusCell = document.createElement("td");
      const status = document.createElement("span");
      const mergeStatusCell = document.createElement("td");
      const remergeCell = document.createElement("td");
      const remergeCheckbox = document.createElement("input");
      const downloaded = isDownloaded(item);
      const selectable = !downloaded;

      checkbox.type = "checkbox";
      checkbox.name = "selected_media_keys";
      checkbox.value = item.key || "";
      checkbox.checked = downloaded || selectedMediaKeys?.has(item.key) || false;
      checkbox.disabled = !selectable || !item.key;
      checkbox.setAttribute("form", "start-form");
      checkbox.setAttribute(
        "aria-label",
        downloaded ? `${item.filename} already downloaded` : `Select ${item.filename}`
      );
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
      size.textContent = formatFileSize(item.file_size);
      size.title = item.file_size ? `${item.file_size} bytes` : "Unknown size";
      capturedAt.textContent = formatCapturedDate(item.captured_at);
      capturedAt.title = formatCapturedDateTime(item.captured_at);
      itemCount.textContent = String(item.item_count ?? 1);
      status.className = `table-status ${item.status}`;
      status.textContent = item.status;

      if (item.merge_status) {
        mergeStatusCell.textContent = mergeStatusLabels[item.merge_status] || item.merge_status;
        mergeStatusCell.className = `table-merge-status ${item.merge_status}`;
      } else {
        mergeStatusCell.textContent = "—";
        mergeStatusCell.className = "table-merge-status not-chaptered";
      }

      remergeCheckbox.type = "checkbox";
      remergeCheckbox.name = "selected_merge_keys";
      remergeCheckbox.value = item.key || "";
      remergeCheckbox.checked = selectedMergeKeys.has(item.key);
      remergeCheckbox.disabled = !item.remerge_eligible || !item.key;
      remergeCheckbox.setAttribute("form", "rerun-merge-form");
      remergeCheckbox.setAttribute(
        "aria-label",
        item.remerge_eligible
          ? `Select ${item.filename} for re-merge`
          : `${item.filename} has nothing to re-merge from`
      );
      remergeCheckbox.addEventListener("change", () => {
        if (remergeCheckbox.checked) {
          selectedMergeKeys.add(item.key);
        } else {
          selectedMergeKeys.delete(item.key);
        }
        updateRerunMergeSummary();
        syncSelectRemergeVisibleState();
        saveSettings();
        document.getElementById("rerun-merge-button")?.toggleAttribute(
          "disabled",
          !selectedMergeKeys.size
        );
      });
      remergeCell.append(remergeCheckbox);

      selectCell.append(checkbox);
      statusCell.append(status);
      row.append(
        selectCell,
        filename,
        size,
        capturedAt,
        itemCount,
        statusCell,
        mergeStatusCell,
        remergeCell
      );
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
    syncSelectRemergeVisibleState();
    updateRerunMergeSummary();
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

  selectRemergeVisible?.addEventListener("change", () => {
    visibleRemergeEligibleItems().forEach((item) => {
      if (selectRemergeVisible.checked) {
        selectedMergeKeys.add(item.key);
      } else {
        selectedMergeKeys.delete(item.key);
      }
    });
    saveSettings();
    render();
    document.getElementById("rerun-merge-button")?.toggleAttribute(
      "disabled",
      !selectedMergeKeys.size
    );
  });

  filesPerBatchInput?.addEventListener("input", saveSettings);
  document.getElementById("start-form")?.addEventListener("submit", saveSettings);
  extensionFilter.addEventListener("change", () => {
    requestedExtensionFilter = extensionFilter.value;
    saveSettings();
    render();
  });
  statusFilter.addEventListener("change", () => {
    requestedStatusFilter = statusFilter.value;
    saveSettings();
    render();
  });

  window.gosyncMediaTable = {
    startFormData(form) {
      saveSettings();
      const formData = new FormData(form);
      const validPendingKeys = pendingKeys();
      const validPendingKeySet = new Set(validPendingKeys);
      const selectedKeys = selectedMediaKeys
        ? Array.from(selectedMediaKeys).filter((key) => validPendingKeySet.has(key))
        : validPendingKeys;

      formData.delete("selected_media_keys");
      formData.delete("selected_media_mode");
      if (selectedKeys.length && selectedKeys.length === validPendingKeys.length) {
        formData.set("selected_media_mode", "all_pending");
      } else {
        selectedKeys.forEach((key) => formData.append("selected_media_keys", key));
      }
      return formData;
    },
    rerunMergeFormData(form) {
      saveSettings();
      const formData = new FormData(form);
      formData.delete("selected_merge_keys");
      Array.from(selectedMergeKeys).forEach((key) => formData.append("selected_merge_keys", key));
      return formData;
    },
    selectedMergeCount() {
      return selectedMergeKeys.size;
    },
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
            .filter((item) => !isDownloaded(item) && item.key)
            .map((item) => item.key)
        );
      } else {
        const validKeys = new Set(
          sidecarItems
            .filter((item) => !isDownloaded(item) && item.key)
            .map((item) => item.key)
        );
        selectedMediaKeys = new Set(
          Array.from(selectedMediaKeys).filter((key) => validKeys.has(key))
        );
      }
      const validMergeKeys = new Set(
        sidecarItems.filter((item) => item.remerge_eligible && item.key).map((item) => item.key)
      );
      selectedMergeKeys = new Set(
        Array.from(selectedMergeKeys).filter((key) => validMergeKeys.has(key))
      );
      syncExtensionFilterOptions();
      syncStatusFilterOptions();
      syncSortButtons();
      render();
      saveSettings();
    },
  };

  syncSortButtons();
}());
