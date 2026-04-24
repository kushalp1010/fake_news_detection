document.addEventListener("DOMContentLoaded", () => {
    const textarea = document.getElementById("news_text");
    const charCount = document.getElementById("charCount");
    const analyzeForm = document.getElementById("analyzeForm");
    const analyzeButton = document.getElementById("analyzeButton");
    const loadingSpinner = document.getElementById("loadingSpinner");
    const themeToggle = document.getElementById("themeToggle");
    const themeToggleLabel = themeToggle?.querySelector(".theme-toggle-label");
    const themeToggleIcon = themeToggle?.querySelector(".theme-toggle-icon");
    const copyResultButton = document.getElementById("copyResultButton");

    const applyThemeLabel = (isDark) => {
        if (!themeToggle) {
            return;
        }

        if (themeToggleLabel) {
            themeToggleLabel.textContent = isDark ? "Light" : "Dark";
        }

        if (themeToggleIcon) {
            themeToggleIcon.textContent = isDark ? "L" : "D";
        }

        themeToggle.setAttribute("aria-label", isDark ? "Switch to light mode" : "Switch to dark mode");
    };

    if (textarea && charCount) {
        const updateCount = () => {
            charCount.textContent = `${textarea.value.length} / 3000 characters`;
        };

        textarea.addEventListener("input", updateCount);
        updateCount();
    }

    if (analyzeForm && analyzeButton && loadingSpinner) {
        analyzeForm.addEventListener("submit", () => {
            analyzeButton.disabled = true;
            loadingSpinner.classList.remove("hidden");
            const buttonText = analyzeButton.querySelector(".button-text");
            if (buttonText) {
                buttonText.textContent = "Analyzing...";
            }
        });
    }

    const savedTheme = localStorage.getItem("theme");
    if (savedTheme === "dark") {
        document.body.classList.add("dark-mode");
    }
    applyThemeLabel(document.body.classList.contains("dark-mode"));

    if (themeToggle) {
        themeToggle.addEventListener("click", () => {
            document.body.classList.toggle("dark-mode");
            const isDark = document.body.classList.contains("dark-mode");
            localStorage.setItem("theme", isDark ? "dark" : "light");
            applyThemeLabel(isDark);
        });
    }

    if (copyResultButton) {
        copyResultButton.addEventListener("click", async () => {
            const resultText = document.querySelector(".result-layout")?.innerText || "";
            try {
                await navigator.clipboard.writeText(resultText);
                copyResultButton.textContent = "Copied";
                setTimeout(() => {
                    copyResultButton.textContent = "Copy Result";
                }, 1500);
            } catch (error) {
                copyResultButton.textContent = "Copy Failed";
            }
        });
    }
});
