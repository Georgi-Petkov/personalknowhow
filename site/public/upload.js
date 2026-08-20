const token = new URLSearchParams(window.location.search).get("token") || "";
const status = document.getElementById("upload-status");

const views = {
  upload: document.getElementById("upload-view"),
  pending_payment: document.getElementById("pending-payment-view"),
  processing: document.getElementById("processing-view"),
  ready: document.getElementById("ready-view"),
  deleted: document.getElementById("deleted-view"),
  rejected: document.getElementById("rejected-view"),
  error: document.getElementById("error-view"),
};

function showView(name) {
  for (const v of Object.values(views)) v.hidden = true;
  views[name].hidden = false;
}

function mcpConfig(mcpUrl, mcpToken) {
  const args = ["mcp-remote", mcpUrl];
  const server = { command: "npx", args };
  if (mcpToken) {
    args.push("--header", "Authorization:${AUTH_HEADER}");
    server.env = { AUTH_HEADER: `Bearer ${mcpToken}` };
  }
  return JSON.stringify({ mcpServers: { personalknowhow: server } }, null, 2);
}

function renderReady(mcpUrlPublic, mcpUrlPrivate, mcpTokenPrivate) {
  document.getElementById("mcp-config-private").textContent = mcpConfig(mcpUrlPrivate, mcpTokenPrivate);
  document.getElementById("mcp-url-public").textContent = mcpUrlPublic;
  document.getElementById("mcp-config-public").textContent = mcpConfig(mcpUrlPublic, null);
}

async function refreshStatus() {
  if (!token) {
    document.getElementById("error-text").textContent = "This link is missing its token.";
    showView("error");
    return;
  }

  try {
    const response = await fetch(`/api/status?token=${encodeURIComponent(token)}`);
    const data = await response.json();

    if (!response.ok) {
      document.getElementById("error-text").textContent = data.error || "This link isn't valid.";
      showView("error");
      return;
    }

    if (data.status === "pending_upload") {
      showView("upload");
    } else if (data.status === "pending_payment") {
      showView("pending_payment");
    } else if (data.status === "processing") {
      showView("processing");
    } else if (data.status === "ready") {
      renderReady(data.mcp_url_public, data.mcp_url_private, data.mcp_token_private);
      showView("ready");
    } else if (data.status === "deleted") {
      showView("deleted");
    } else if (data.status === "rejected") {
      document.getElementById("rejected-reason").textContent = data.reason || "";
      showView("rejected");
    }
  } catch {
    document.getElementById("error-text").textContent = "Network error. Reload to try again.";
    showView("error");
  }
}

function updateSubmitState() {
  const form = document.getElementById("upload-form");
  const hasFile = form.file.files.length > 0;
  const hasConsent = form.consent.checked;
  // type="email" gives us free format validation via the native constraint API --
  // valid when empty (referred_by isn't required) or when it looks like an email.
  const referredByOk = form.referred_by.checkValidity();
  form.querySelector("button[type=submit]").disabled = !(hasFile && hasConsent && referredByOk);
}

const uploadForm = document.getElementById("upload-form");
uploadForm.file.addEventListener("change", updateSubmitState);
uploadForm.consent.addEventListener("change", updateSubmitState);
uploadForm.referred_by.addEventListener("input", updateSubmitState);

document.getElementById("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const file = form.file.files[0];
  const submitButton = form.querySelector("button[type=submit]");

  if (!file) {
    status.textContent = "Choose a file first.";
    return;
  }
  if (!form.consent.checked) {
    status.textContent = "You need to accept the data-retention terms to upload.";
    return;
  }

  submitButton.disabled = true;
  status.textContent = "Uploading…";

  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("consent", "true");
    if (form.referred_by.value.trim()) {
      formData.append("referred_by", form.referred_by.value.trim());
    }

    const response = await fetch("/api/upload", {
      method: "POST",
      headers: { "X-Upload-Token": token },
      body: formData,
    });
    const data = await response.json();

    if (response.ok) {
      status.textContent = "";
      await refreshStatus();
    } else {
      status.textContent = data.error || "Something went wrong. Try again.";
      submitButton.disabled = false;
    }
  } catch {
    status.textContent = "Network error. Try again.";
    submitButton.disabled = false;
  }
});

const deleteConfirmSection = document.getElementById("delete-confirm");
const deleteEmailInput = document.getElementById("delete-email");
const deleteConfirmButton = document.getElementById("delete-confirm-button");

document.getElementById("delete-button").addEventListener("click", () => {
  document.getElementById("delete-button").hidden = true;
  deleteConfirmSection.hidden = false;
  deleteEmailInput.focus();
});

document.getElementById("delete-cancel").addEventListener("click", () => {
  deleteConfirmSection.hidden = true;
  document.getElementById("delete-button").hidden = false;
  deleteEmailInput.value = "";
  deleteConfirmButton.disabled = true;
});

deleteEmailInput.addEventListener("input", () => {
  deleteConfirmButton.disabled = deleteEmailInput.value.trim().length === 0;
});

deleteConfirmButton.addEventListener("click", async () => {
  const email = deleteEmailInput.value.trim();
  if (!email) return;

  deleteConfirmButton.disabled = true;

  try {
    const response = await fetch("/api/delete-data", {
      method: "POST",
      headers: { "X-Upload-Token": token, "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    const data = await response.json();

    if (response.ok) {
      showView("deleted");
    } else {
      status.textContent = data.error || "Couldn't delete. Try again.";
      deleteConfirmButton.disabled = false;
    }
  } catch {
    status.textContent = "Network error. Try again.";
    deleteConfirmButton.disabled = false;
  }
});

refreshStatus();
