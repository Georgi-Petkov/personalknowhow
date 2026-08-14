const token = new URLSearchParams(window.location.search).get("token") || "";
const status = document.getElementById("upload-status");

const views = {
  upload: document.getElementById("upload-view"),
  processing: document.getElementById("processing-view"),
  ready: document.getElementById("ready-view"),
  deleted: document.getElementById("deleted-view"),
  error: document.getElementById("error-view"),
};

function showView(name) {
  for (const v of Object.values(views)) v.hidden = true;
  views[name].hidden = false;
}

function renderReady(mcpUrl, mcpToken) {
  const config = {
    mcpServers: {
      personalknowhow: {
        command: "npx",
        args: ["mcp-remote", mcpUrl, "--header", "Authorization:${AUTH_HEADER}"],
        env: { AUTH_HEADER: `Bearer ${mcpToken}` },
      },
    },
  };
  document.getElementById("mcp-config").textContent = JSON.stringify(config, null, 2);
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
    } else if (data.status === "processing") {
      showView("processing");
    } else if (data.status === "ready") {
      renderReady(data.mcp_url, data.mcp_token);
      showView("ready");
    } else if (data.status === "deleted") {
      showView("deleted");
    }
  } catch {
    document.getElementById("error-text").textContent = "Network error. Reload to try again.";
    showView("error");
  }
}

document.getElementById("upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const file = form.file.files[0];
  const submitButton = form.querySelector("button[type=submit]");

  if (!file) {
    status.textContent = "Choose a file first.";
    return;
  }

  submitButton.disabled = true;
  status.textContent = "Uploading…";

  try {
    const formData = new FormData();
    formData.append("file", file);

    const response = await fetch("/api/upload", {
      method: "POST",
      headers: { "X-Upload-Token": token },
      body: formData,
    });
    const data = await response.json();

    if (response.ok) {
      status.textContent = "";
      showView("processing");
    } else {
      status.textContent = data.error || "Something went wrong. Try again.";
      submitButton.disabled = false;
    }
  } catch {
    status.textContent = "Network error. Try again.";
    submitButton.disabled = false;
  }
});

document.getElementById("delete-button").addEventListener("click", async () => {
  if (!confirm("This permanently deletes your uploaded export and processed data. Continue?")) {
    return;
  }

  const button = document.getElementById("delete-button");
  button.disabled = true;

  try {
    const response = await fetch("/api/delete-data", {
      method: "POST",
      headers: { "X-Upload-Token": token },
    });
    const data = await response.json();

    if (response.ok) {
      showView("deleted");
    } else {
      status.textContent = data.error || "Couldn't delete. Try again.";
      button.disabled = false;
    }
  } catch {
    status.textContent = "Network error. Try again.";
    button.disabled = false;
  }
});

refreshStatus();
