const costChart = document.querySelector(".cost-chart");
const costBins = [...costChart.querySelectorAll(".cost-bin")];
const costDetail = costChart.querySelector("#cost-chart-detail");

function showCostDetail(bin) {
    for (const item of costBins) item.classList.toggle("is-active", item === bin);
    costDetail.textContent = bin.getAttribute("aria-label");
}

function focusCostBin(bin) {
    bin.focus({ preventScroll: true });
    bin.scrollIntoView({ block: "nearest", inline: "nearest" });
    showCostDetail(bin);
}

for (const [index, bin] of costBins.entries()) {
    bin.addEventListener("pointerenter", () => showCostDetail(bin));
    bin.addEventListener("focus", () => {
        for (const item of costBins) item.tabIndex = item === bin ? 0 : -1;
        showCostDetail(bin);
    });
    bin.addEventListener("click", () => focusCostBin(bin));
    bin.addEventListener("keydown", (event) => {
        let next = index;
        switch (event.key) {
            case "ArrowLeft": next = Math.max(0, index - 1); break;
            case "ArrowRight": next = Math.min(costBins.length - 1, index + 1); break;
            case "Home": next = 0; break;
            case "End": next = costBins.length - 1; break;
            case "Enter":
            case " ": break;
            default: return;
        }
        event.preventDefault();
        focusCostBin(costBins[next]);
    });
}
