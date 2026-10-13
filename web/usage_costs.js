"use strict";

(function (root) {
  const usageCosts = Object.freeze({
    totalsNote: "(ohne unbekannte)",
    formatCost(cost, provider, formatCurrency = value => "$" + Number(value).toFixed(2)) {
      if (cost === null || cost === undefined) return "unbekannt";
      if (provider === "ollama" && Number(cost) === 0) return "lokal";
      return formatCurrency(cost);
    },
  });

  root.MantisUsageCosts = usageCosts;
  if (typeof module !== "undefined" && module.exports) module.exports = usageCosts;
})(typeof window === "undefined" ? globalThis : window);
