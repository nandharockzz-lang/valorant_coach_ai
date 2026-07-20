window.CoachUi = {
  formatBytes(value) {
    return `${Math.round(Number(value || 0) / 1024 / 1024)} MB`;
  },
  toLines(value) {
    if (Array.isArray(value)) {
      return value.join("\n");
    }
    return String(value || "");
  },
};
