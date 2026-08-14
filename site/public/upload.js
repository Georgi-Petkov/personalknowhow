const form = document.getElementById("upload-form");
const status = document.getElementById("upload-status");
const token = new URLSearchParams(window.location.search).get("token") || "";

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = form.file.files[0];
  const submitButton = form.querySelector("button[type=submit]");

  if (!token) {
    status.textContent = "This link is missing its upload token.";
    return;
  }
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
      status.textContent = "Received — I'll be in touch once it's processed.";
      form.reset();
    } else {
      status.textContent = data.error || "Something went wrong. Try again.";
      submitButton.disabled = false;
    }
  } catch {
    status.textContent = "Network error. Try again.";
    submitButton.disabled = false;
  }
});
