const STATUS_STYLES = Object.freeze({
  busy: { icon: "waiting", color: "#39755F" },
  ready: { icon: "success", color: "#39755F" },
  warn: { icon: "warn", color: "#A87832" },
  done: { icon: "success", color: "#737773" },
  muted: { icon: "info", color: "#737773" },
});

Component({
  properties: {
    text: { type: String, value: "" },
    tone: { type: String, value: "muted", observer: "syncStyle" },
    compact: { type: Boolean, value: false },
  },
  data: {
    iconType: "info",
    iconColor: "#737773",
  },
  lifetimes: {
    attached() {
      this.syncStyle();
    },
  },
  methods: {
    syncStyle() {
      const style = STATUS_STYLES[this.data.tone] || STATUS_STYLES.muted;
      this.setData({ iconType: style.icon, iconColor: style.color });
    },
  },
});
