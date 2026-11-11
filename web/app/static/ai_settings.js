const settingsForm = document.querySelector("#ai-settings-form");
const modelInput = document.querySelector("#pitt-model");
const keyInput = document.querySelector("#pitt-api-key");
const testButton = document.querySelector("#test-pitt-api");
const saveButton = document.querySelector("#save-ai-settings");
const testResult = document.querySelector("#pitt-test-result");
const settingsAvailable = !saveButton.disabled;

function clearTestResult() {
    testResult.textContent = "";
    testResult.hidden = true;
    testResult.className = "";
}

function showTestResult(message, ok) {
    testResult.textContent = message;
    testResult.className = ok ? "form-success" : "form-error";
    testResult.hidden = false;
}

modelInput.addEventListener("change", clearTestResult);
keyInput.addEventListener("input", clearTestResult);
window.addEventListener("pageshow", () => {
    keyInput.value = "";
    clearTestResult();
    if (settingsAvailable) {
        for (const control of [modelInput, keyInput, testButton, saveButton]) control.disabled = false;
    }
});

testButton.addEventListener("click", async () => {
    if (!settingsForm.reportValidity()) return;
    const body = new FormData(settingsForm);
    clearTestResult();
    for (const control of [modelInput, keyInput, testButton, saveButton]) control.disabled = true;
    settingsForm.setAttribute("aria-busy", "true");
    testButton.textContent = "Testing…";
    try {
        const response = await fetch("/settings/ai/test", {
            method: "POST", body, credentials: "same-origin", cache: "no-store",
        });
        if (response.redirected) {
            showTestResult("Your session has expired. Reload this page to sign in.", false);
            return;
        }
        const result = await response.json();
        if (typeof result.message !== "string" || typeof result.ok !== "boolean") throw new Error();
        showTestResult(result.message, response.ok && result.ok);
    } catch {
        showTestResult("The API test could not be completed. Try again later.", false);
    } finally {
        for (const control of [modelInput, keyInput, testButton, saveButton]) control.disabled = false;
        settingsForm.removeAttribute("aria-busy");
        testButton.textContent = "Test API";
    }
});

settingsForm.addEventListener("submit", () => {
    testButton.disabled = true;
    saveButton.disabled = true;
});
