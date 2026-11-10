try {
  var t = localStorage.getItem("agentops.theme");
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
} catch (e) {
  /* storage blocked: follow the system setting */
}
