(() => {
  "use strict";

  const basePath = document.body.dataset.base || "";
  const THEME_KEY = "appdaemon_store_theme";
  const THEMES = ["auto", "light", "dark"];

  const dom = {
    toastContainer: document.getElementById("toast-container"),
    runtimeBadge: document.getElementById("runtime-badge"),
    appsDirValue: document.getElementById("apps-dir-value"),
    configSourcePill: document.getElementById("config-source-pill"),
    appsDirMissing: document.getElementById("apps-dir-missing"),
    lastCheckedValue: document.getElementById("last-checked-value"),
    themeToggleBtn: document.getElementById("theme-toggle-btn"),
    checkUpdatesBtn: document.getElementById("check-updates-btn"),
    migrationsSection: document.getElementById("migrations-section"),
    migrationsList: document.getElementById("migrations-list"),
    addRepoForm: document.getElementById("add-repo-form"),
    repoInput: document.getElementById("repo-input"),
    addAutoUpdate: document.getElementById("add-auto-update"),
    addRepoBtn: document.getElementById("add-repo-btn"),
    repoCountBadge: document.getElementById("repo-count-badge"),
    reposEmpty: document.getElementById("repos-empty"),
    reposList: document.getElementById("repos-list"),
    requirementsEmpty: document.getElementById("requirements-empty"),
    requirementsList: document.getElementById("requirements-list"),
    supervisorReqsPanel: document.getElementById("supervisor-reqs-panel"),
    supervisorReqsStatus: document.getElementById("supervisor-reqs-status"),
    supervisorReqsDiff: document.getElementById("supervisor-reqs-diff"),
    syncRestartCheckbox: document.getElementById("sync-restart-checkbox"),
    syncReqsBtn: document.getElementById("sync-reqs-btn"),
    standaloneReqsPanel: document.getElementById("standalone-reqs-panel"),
    standaloneReqsCmd: document.getElementById("standalone-reqs-cmd"),
    confirmModal: document.getElementById("confirm-modal"),
    confirmTitle: document.getElementById("confirm-title"),
    confirmMessage: document.getElementById("confirm-message"),
    confirmDetail: document.getElementById("confirm-detail"),
    confirmCancelBtn: document.getElementById("confirm-cancel-btn"),
    confirmAcceptBtn: document.getElementById("confirm-accept-btn"),
  };

  let pendingConfirmAction = null;

  function createEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function showToast(message, kind) {
    const toast = createEl("div", `toast toast-${kind || "info"}`, message);
    dom.toastContainer.appendChild(toast);
    window.setTimeout(() => {
      toast.remove();
    }, 4500);
  }

  function setBtnBusy(button, isBusy) {
    if (!button) {
      return;
    }
    button.disabled = isBusy;
    const spinner = button.querySelector(".spinner");
    if (spinner) {
      spinner.hidden = !isBusy;
    }
  }

  function formatTimestamp(raw) {
    if (!raw) {
      return "Never";
    }
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) {
      return raw;
    }
    return parsed.toLocaleString();
  }

  async function apiRequest(path, method, payload) {
    const options = {
      method: method || "GET",
      headers: {},
    };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }

    const response = await fetch(`${basePath}${path}`, options);
    const text = await response.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_err) {
        data = { detail: text };
      }
    }

    if (!response.ok) {
      const detail = data && data.detail ? data.detail : `Request failed (${response.status})`;
      throw new Error(detail);
    }

    return data;
  }

  function splitRepoId(repoId) {
    const parts = repoId.split("/");
    return {
      owner: encodeURIComponent(parts[0] || ""),
      repo: encodeURIComponent(parts[1] || ""),
    };
  }

  function applyTheme(theme) {
    const valid = THEMES.includes(theme) ? theme : "auto";
    if (valid === "auto") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", valid);
    }
    const label = valid.charAt(0).toUpperCase() + valid.slice(1);
    dom.themeToggleBtn.textContent = `Theme: ${label}`;
  }

  function initTheme() {
    let saved = "auto";
    try {
      saved = window.localStorage.getItem(THEME_KEY) || "auto";
    } catch (_err) {
      saved = "auto";
    }
    applyTheme(saved);

    dom.themeToggleBtn.addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme") || "auto";
      const nextIdx = (THEMES.indexOf(current) + 1) % THEMES.length;
      const nextTheme = THEMES[nextIdx];
      applyTheme(nextTheme);
      try {
        window.localStorage.setItem(THEME_KEY, nextTheme);
      } catch (_err) {
        // Ignore storage errors inside restricted iframe contexts.
      }
    });
  }

  function openConfirmModal(opts) {
    dom.confirmTitle.textContent = opts.title;
    dom.confirmMessage.textContent = opts.message;
    if (opts.detail) {
      dom.confirmDetail.textContent = opts.detail;
      dom.confirmDetail.hidden = false;
    } else {
      dom.confirmDetail.textContent = "";
      dom.confirmDetail.hidden = true;
    }
    dom.confirmAcceptBtn.textContent = opts.confirmLabel || "Confirm";
    dom.confirmAcceptBtn.className = `btn ${opts.confirmClass || "btn-danger"}`;
    pendingConfirmAction = opts.onConfirm;
    dom.confirmModal.hidden = false;
    dom.confirmCancelBtn.focus();
  }

  function closeConfirmModal() {
    dom.confirmModal.hidden = true;
    pendingConfirmAction = null;
  }

  function renderHeader(status) {
    const isHaos = status.runtime_mode === "haos";
    dom.runtimeBadge.textContent = isHaos ? "HA-OS" : "Standalone";
    dom.runtimeBadge.className = isHaos ? "badge badge-accent" : "badge badge-neutral";

    dom.appsDirValue.textContent = status.apps_dir;
    dom.configSourcePill.textContent = status.config_source;
    dom.appsDirMissing.hidden = Boolean(status.apps_dir_exists);
    dom.lastCheckedValue.textContent = formatTimestamp(status.last_global_check);
  }

  function migrationActionLabel(type) {
    if (type === "legacy_path") {
      return "Migrate Path";
    }
    if (type === "duplicate_app") {
      return "Remove Duplicate";
    }
    return "Remove Folder";
  }

  function renderMigrations(migrations) {
    dom.migrationsList.textContent = "";
    if (!migrations || migrations.length === 0) {
      dom.migrationsSection.hidden = true;
      return;
    }

    dom.migrationsSection.hidden = false;
    for (const cand of migrations) {
      const card = createEl("div", "item-card");

      const top = createEl("div", "repo-top");
      const titleGroup = createEl("div", "repo-title-group");
      titleGroup.appendChild(createEl("strong", "", cand.title));
      titleGroup.appendChild(createEl("span", "badge badge-warn", cand.migration_type));
      if (cand.related_repo) {
        titleGroup.appendChild(createEl("span", "badge badge-neutral", cand.related_repo));
      }
      top.appendChild(titleGroup);
      card.appendChild(top);

      card.appendChild(createEl("p", "card-subtitle", cand.description));

      const pathLine = createEl("div", "path-line");
      pathLine.appendChild(createEl("span", "", "Source:"));
      pathLine.appendChild(createEl("code", "code-pill", cand.source_path));
      if (cand.target_path) {
        pathLine.appendChild(createEl("span", "", "-> Target:"));
        pathLine.appendChild(createEl("code", "code-pill", cand.target_path));
      }
      card.appendChild(pathLine);

      if (cand.items && cand.items.length > 0) {
        const itemsBlock = createEl("div", "meta-block");
        itemsBlock.appendChild(createEl("span", "meta-label", "Affected items:"));
        const chips = createEl("div", "chip-list");
        for (const item of cand.items) {
          chips.appendChild(createEl("span", "chip", item));
        }
        itemsBlock.appendChild(chips);
        card.appendChild(itemsBlock);
      }

      const controls = createEl("div", "repo-controls");
      const actionGroup = createEl("div", "control-group");
      const actionLabel = migrationActionLabel(cand.migration_type);
      const isLegacy = cand.migration_type === "legacy_path";
      const applyBtn = createEl(
        "button",
        `btn btn-sm ${isLegacy ? "btn-primary" : "btn-danger"}`,
        actionLabel
      );
      applyBtn.type = "button";
      applyBtn.addEventListener("click", () => {
        const detailText = cand.target_path
          ? `${cand.source_path} -> ${cand.target_path}`
          : cand.source_path;
        openConfirmModal({
          title: actionLabel,
          message: isLegacy
            ? "Copy legacy apps and update appdaemon.yaml to the new path?"
            : "Permanently remove this directory from apps_dir?",
          detail: detailText,
          confirmLabel: actionLabel,
          confirmClass: isLegacy ? "btn-primary" : "btn-danger",
          onConfirm: async () => {
            setBtnBusy(applyBtn, true);
            try {
              const updated = await apiRequest("/api/migrations/apply", "POST", {
                candidate_id: cand.id,
              });
              showToast(`Applied: ${cand.title}`, "success");
              await renderAll(updated);
            } catch (err) {
              showToast(err.message, "error");
            } finally {
              setBtnBusy(applyBtn, false);
            }
          },
        });
      });

      const dismissBtn = createEl("button", "btn btn-sm btn-secondary", "Dismiss");
      dismissBtn.type = "button";
      dismissBtn.addEventListener("click", async () => {
        setBtnBusy(dismissBtn, true);
        try {
          const updated = await apiRequest("/api/migrations/dismiss", "POST", {
            candidate_id: cand.id,
          });
          showToast("Migration notice dismissed", "info");
          await renderAll(updated);
        } catch (err) {
          showToast(err.message, "error");
        } finally {
          setBtnBusy(dismissBtn, false);
        }
      });

      actionGroup.appendChild(applyBtn);
      actionGroup.appendChild(dismissBtn);
      controls.appendChild(actionGroup);
      card.appendChild(controls);
      dom.migrationsList.appendChild(card);
    }
  }

  function buildMetaChips(label, values) {
    const block = createEl("div", "meta-block");
    block.appendChild(createEl("span", "meta-label", label));
    const chips = createEl("div", "chip-list");
    for (const val of values) {
      chips.appendChild(createEl("span", "chip", val));
    }
    block.appendChild(chips);
    return block;
  }

  function renderRepos(repositories) {
    dom.reposList.textContent = "";
    const count = repositories ? repositories.length : 0;
    dom.repoCountBadge.textContent = String(count);

    if (!repositories || repositories.length === 0) {
      dom.reposEmpty.hidden = false;
      return;
    }

    dom.reposEmpty.hidden = true;
    for (const repo of repositories) {
      const cardClass = repo.update_available ? "item-card item-card-highlight" : "item-card";
      const card = createEl("div", cardClass);

      const top = createEl("div", "repo-top");
      const titleGroup = createEl("div", "repo-title-group");

      const link = createEl("a", "repo-link", repo.id);
      link.href = repo.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      titleGroup.appendChild(link);

      const installedVer = repo.installed_version || "Not installed";
      titleGroup.appendChild(createEl("span", "badge badge-neutral", `Installed: ${installedVer}`));

      if (repo.update_available && repo.latest_version) {
        titleGroup.appendChild(
          createEl("span", "badge badge-warn", `Update: ${repo.latest_version}`)
        );
      } else if (repo.latest_version) {
        titleGroup.appendChild(
          createEl("span", "badge badge-success", `Latest: ${repo.latest_version}`)
        );
      }

      if (repo.version_kind) {
        titleGroup.appendChild(createEl("span", "badge badge-accent", repo.version_kind));
      }
      if (repo.pinned_ref) {
        titleGroup.appendChild(createEl("span", "badge badge-subtle", `pin: ${repo.pinned_ref}`));
      }
      titleGroup.appendChild(createEl("span", "badge badge-subtle", repo.origin));

      top.appendChild(titleGroup);
      card.appendChild(top);

      if (repo.last_error) {
        card.appendChild(createEl("div", "error-banner", repo.last_error));
      }

      const metaGrid = createEl("div", "repo-meta-grid");
      if (repo.installed_folders && repo.installed_folders.length > 0) {
        metaGrid.appendChild(buildMetaChips("Folders:", repo.installed_folders));
      }
      if (repo.requirements && repo.requirements.length > 0) {
        metaGrid.appendChild(buildMetaChips("Requirements:", repo.requirements));
      }
      metaGrid.appendChild(
        createEl("span", "", `Checked: ${formatTimestamp(repo.last_checked)}`)
      );
      card.appendChild(metaGrid);

      const controls = createEl("div", "repo-controls");
      const leftControls = createEl("div", "control-group");
      const rightControls = createEl("div", "control-group");
      const { owner, repo: repoSlug } = splitRepoId(repo.id);

      const updateBtn = createEl(
        "button",
        `btn btn-sm ${repo.update_available ? "btn-primary" : "btn-secondary"}`,
        "Update"
      );
      updateBtn.type = "button";
      updateBtn.addEventListener("click", async () => {
        setBtnBusy(updateBtn, true);
        try {
          await apiRequest(`/api/repos/${owner}/${repoSlug}/update`, "POST");
          showToast(`Updated ${repo.id}`, "success");
          await refreshStatus();
        } catch (err) {
          showToast(err.message, "error");
        } finally {
          setBtnBusy(updateBtn, false);
        }
      });

      const reinstallBtn = createEl("button", "btn btn-sm btn-secondary", "Reinstall");
      reinstallBtn.type = "button";
      reinstallBtn.addEventListener("click", async () => {
        setBtnBusy(reinstallBtn, true);
        try {
          await apiRequest(`/api/repos/${owner}/${repoSlug}/reinstall`, "POST");
          showToast(`Reinstalled ${repo.id}`, "success");
          await refreshStatus();
        } catch (err) {
          showToast(err.message, "error");
        } finally {
          setBtnBusy(reinstallBtn, false);
        }
      });

      const pinInput = createEl("input", "input input-sm");
      pinInput.type = "text";
      pinInput.placeholder = "Pin tag / branch / sha";
      pinInput.value = repo.pinned_ref || "";
      pinInput.setAttribute("aria-label", `Pin ref for ${repo.id}`);

      const pinBtn = createEl("button", "btn btn-sm btn-secondary", "Pin");
      pinBtn.type = "button";
      pinBtn.addEventListener("click", async () => {
        const refVal = pinInput.value.trim();
        setBtnBusy(pinBtn, true);
        try {
          await apiRequest(`/api/repos/${owner}/${repoSlug}/pin`, "POST", {
            pinned_ref: refVal || null,
          });
          showToast(
            refVal ? `Pinned ${repo.id} to ${refVal}` : `Unpinned ${repo.id}`,
            "success"
          );
          await refreshStatus();
        } catch (err) {
          showToast(err.message, "error");
        } finally {
          setBtnBusy(pinBtn, false);
        }
      });

      leftControls.appendChild(updateBtn);
      leftControls.appendChild(reinstallBtn);
      leftControls.appendChild(pinInput);
      leftControls.appendChild(pinBtn);

      if (repo.pinned_ref) {
        const unpinBtn = createEl("button", "btn btn-sm btn-secondary", "Unpin");
        unpinBtn.type = "button";
        unpinBtn.addEventListener("click", async () => {
          setBtnBusy(unpinBtn, true);
          try {
            await apiRequest(`/api/repos/${owner}/${repoSlug}/pin`, "POST", {
              pinned_ref: null,
            });
            showToast(`Unpinned ${repo.id}`, "success");
            await refreshStatus();
          } catch (err) {
            showToast(err.message, "error");
          } finally {
            setBtnBusy(unpinBtn, false);
          }
        });
        leftControls.appendChild(unpinBtn);
      }

      const switchLabel = createEl("label", "switch-label");
      const switchInput = createEl("input", "switch-input");
      switchInput.type = "checkbox";
      switchInput.checked = repo.auto_update === "enabled";
      switchInput.addEventListener("change", async () => {
        const nextMode = switchInput.checked ? "enabled" : "disabled";
        switchInput.disabled = true;
        try {
          await apiRequest(`/api/repos/${owner}/${repoSlug}/auto-update`, "POST", {
            auto_update: nextMode,
          });
          showToast(`Auto-update ${nextMode} for ${repo.id}`, "success");
          await refreshStatus();
        } catch (err) {
          switchInput.checked = !switchInput.checked;
          showToast(err.message, "error");
        } finally {
          switchInput.disabled = false;
        }
      });
      switchLabel.appendChild(switchInput);
      switchLabel.appendChild(createEl("span", "", "Auto-update"));

      const removeBtn = createEl("button", "btn btn-sm btn-danger", "Remove");
      removeBtn.type = "button";
      removeBtn.addEventListener("click", () => {
        const folderSummary = repo.installed_folders && repo.installed_folders.length > 0
          ? `Installed folders to delete: ${repo.installed_folders.join(", ")}`
          : "";
        openConfirmModal({
          title: "Remove Repository",
          message: `Remove ${repo.id} and delete its installed app folders?`,
          detail: folderSummary,
          confirmLabel: "Remove",
          confirmClass: "btn-danger",
          onConfirm: async () => {
            setBtnBusy(removeBtn, true);
            try {
              await apiRequest(`/api/repos/${owner}/${repoSlug}`, "DELETE");
              showToast(`Removed ${repo.id}`, "success");
              await refreshStatus();
            } catch (err) {
              showToast(err.message, "error");
            } finally {
              setBtnBusy(removeBtn, false);
            }
          },
        });
      });

      rightControls.appendChild(switchLabel);
      rightControls.appendChild(removeBtn);

      controls.appendChild(leftControls);
      controls.appendChild(rightControls);
      card.appendChild(controls);
      dom.reposList.appendChild(card);
    }
  }

  function renderSupervisorDiff(preview) {
    dom.supervisorReqsDiff.textContent = "";
    dom.supervisorReqsStatus.textContent = preview.message || "";

    const missingSet = new Set(preview.missing_packages || []);
    const required = preview.required_packages || [];
    for (const pkg of required) {
      const isMissing = missingSet.has(pkg);
      const chip = createEl(
        "span",
        `chip ${isMissing ? "chip-missing" : "chip-ok"}`,
        `${pkg} (${isMissing ? "missing" : "configured"})`
      );
      dom.supervisorReqsDiff.appendChild(chip);
    }

    dom.syncReqsBtn.disabled = !preview.missing_packages || preview.missing_packages.length === 0;
  }

  async function renderRequirements(status) {
    const reqs = status.all_requirements || [];
    dom.requirementsList.textContent = "";

    if (reqs.length === 0) {
      dom.requirementsEmpty.hidden = false;
    } else {
      dom.requirementsEmpty.hidden = true;
      for (const req of reqs) {
        dom.requirementsList.appendChild(createEl("span", "chip", req));
      }
    }

    if (status.supervisor_available) {
      dom.supervisorReqsPanel.hidden = false;
      dom.standaloneReqsPanel.hidden = true;
      dom.supervisorReqsStatus.textContent = "Checking AppDaemon add-on python_packages...";
      dom.supervisorReqsDiff.textContent = "";
      try {
        const preview = await apiRequest("/api/requirements/preview", "GET");
        renderSupervisorDiff(preview);
      } catch (err) {
        dom.supervisorReqsStatus.textContent = `Supervisor preview error: ${err.message}`;
        dom.syncReqsBtn.disabled = reqs.length === 0;
      }
    } else {
      dom.supervisorReqsPanel.hidden = true;
      dom.standaloneReqsPanel.hidden = false;
      if (reqs.length > 0) {
        dom.standaloneReqsCmd.hidden = false;
        dom.standaloneReqsCmd.textContent = `pip install ${reqs.join(" ")}`;
      } else {
        dom.standaloneReqsCmd.hidden = true;
        dom.standaloneReqsCmd.textContent = "";
      }
    }
  }

  async function renderAll(status) {
    renderHeader(status);
    renderMigrations(status.migrations);
    renderRepos(status.repositories);
    await renderRequirements(status);
  }

  async function refreshStatus() {
    const status = await apiRequest("/api/status", "GET");
    await renderAll(status);
  }

  function bindEvents() {
    dom.checkUpdatesBtn.addEventListener("click", async () => {
      setBtnBusy(dom.checkUpdatesBtn, true);
      try {
        const status = await apiRequest("/api/check-updates", "POST");
        await renderAll(status);
        showToast("Update check completed", "success");
      } catch (err) {
        showToast(err.message, "error");
      } finally {
        setBtnBusy(dom.checkUpdatesBtn, false);
      }
    });

    dom.addRepoForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const repoVal = dom.repoInput.value.trim();
      if (!repoVal) {
        return;
      }

      setBtnBusy(dom.addRepoBtn, true);
      try {
        const mode = dom.addAutoUpdate.checked ? "enabled" : "disabled";
        const created = await apiRequest("/api/repos", "POST", {
          repo: repoVal,
          auto_update: mode,
        });
        dom.repoInput.value = "";
        showToast(`Installed ${created.id}`, "success");
        await refreshStatus();
      } catch (err) {
        showToast(err.message, "error");
      } finally {
        setBtnBusy(dom.addRepoBtn, false);
      }
    });

    dom.syncReqsBtn.addEventListener("click", async () => {
      setBtnBusy(dom.syncReqsBtn, true);
      try {
        const restartMode = dom.syncRestartCheckbox.checked ? "restart" : "no_restart";
        const result = await apiRequest("/api/requirements/sync", "POST", {
          restart: restartMode,
        });
        renderSupervisorDiff(result);
        showToast(result.message || "Packages synced", "success");
      } catch (err) {
        showToast(err.message, "error");
      } finally {
        setBtnBusy(dom.syncReqsBtn, false);
      }
    });

    dom.confirmCancelBtn.addEventListener("click", closeConfirmModal);
    dom.confirmModal.addEventListener("click", (event) => {
      if (event.target === dom.confirmModal) {
        closeConfirmModal();
      }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !dom.confirmModal.hidden) {
        closeConfirmModal();
      }
    });
    dom.confirmAcceptBtn.addEventListener("click", async () => {
      const action = pendingConfirmAction;
      closeConfirmModal();
      if (action) {
        await action();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    initTheme();
    bindEvents();
    try {
      await refreshStatus();
    } catch (err) {
      showToast(err.message, "error");
    }
  });
})();
