(function () {
  "use strict";

  const CURRENCY_FORMATTER = new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    maximumFractionDigits: 0,
  });

  function formatPrice(plan) {
    if (plan.price_ars === null || plan.price_ars === undefined) {
      return plan.price_formatted || "Consultar";
    }
    if (plan.price_ars === 0) {
      return plan.price_formatted || "Sin costo";
    }
    if (plan.price_formatted) {
      return plan.price_formatted;
    }
    try {
      return CURRENCY_FORMATTER.format(plan.price_ars);
    } catch (error) {
      console.warn("[plans] No se pudo formatear el precio", error);
      return `$${plan.price_ars}`;
    }
  }

  function formatLimit(plan) {
    if (plan.message_limit === null || plan.message_limit === undefined) {
      return plan.message_limit_formatted || "Interacciones ilimitadas";
    }
    if (plan.message_limit_formatted) {
      return plan.message_limit_formatted;
    }
    return `Hasta ${plan.message_limit} interacciones/mes`;
  }

  function renderList(container, items) {
    if (!container) {
      return;
    }
    container.innerHTML = "";
    if (!Array.isArray(items) || !items.length) {
      return;
    }
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      container.appendChild(li);
    }
  }

  function applyPlanData(card, plan) {
    if (!card || !plan) {
      return;
    }

    const priceEl = card.querySelector("[data-plan-price]");
    if (priceEl) {
      priceEl.textContent = formatPrice(plan);
    }

    const limitEl = card.querySelector("[data-plan-limit]");
    if (limitEl) {
      limitEl.textContent = formatLimit(plan);
    }

    const summaryEl = card.querySelector("[data-plan-summary]");
    if (summaryEl && plan.summary) {
      summaryEl.textContent = plan.summary;
    }

    const badgeEl = card.querySelector("[data-plan-badge]");
    if (badgeEl) {
      badgeEl.textContent = plan.badge || "";
      badgeEl.style.display = plan.badge ? "inline-flex" : "none";
    }

    const ctaEl = card.querySelector("[data-plan-cta]");
    if (ctaEl && plan.cta_label) {
      ctaEl.textContent = plan.cta_label;
    }

    renderList(card.querySelector("[data-plan-features]"), plan.features);
    renderList(card.querySelector("[data-plan-technologies]"), plan.technologies);
  }

  async function fetchPlanCatalog() {
    try {
      const response = await fetch("/auth/plans", { credentials: "include" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const payload = await response.json();
      const plans = payload.planes || payload.plans || [];
      return Array.isArray(plans) ? plans : [];
    } catch (error) {
      console.error("[plans] No se pudieron cargar los planes", error);
      return [];
    }
  }

  async function hydratePlans() {
    const cards = document.querySelectorAll("[data-plan-card]");
    if (!cards.length) {
      return;
    }

    const catalog = await fetchPlanCatalog();
    if (!catalog.length) {
      return;
    }

    const planMap = new Map(catalog.map((plan) => [String(plan.key).toLowerCase(), plan]));

    cards.forEach((card) => {
      const key = String(card.getAttribute("data-plan-card") || "").toLowerCase();
      if (!planMap.has(key)) {
        return;
      }
      applyPlanData(card, planMap.get(key));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hydratePlans);
  } else {
    hydratePlans();
  }
})();
