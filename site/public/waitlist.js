const form = document.getElementById("waitlist-form");
const status = document.getElementById("waitlist-status");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = form.email.value.trim();
  const note = form.note.value.trim();
  const submitButton = form.querySelector("button[type=submit]");

  submitButton.disabled = true;
  status.textContent = "Submitting…";

  try {
    const response = await fetch("/api/waitlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, note }),
    });
    const data = await response.json();

    if (response.ok) {
      status.textContent = "You're on the list — thanks.";
      form.reset();
    } else {
      status.textContent = data.error || "Something went wrong. Try again.";
    }
  } catch {
    status.textContent = "Network error. Try again.";
  } finally {
    submitButton.disabled = false;
  }
});
