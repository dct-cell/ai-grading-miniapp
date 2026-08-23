Component({
  properties: {
    title: { type: String, value: "数学竞赛题批改" },
    back: { type: Boolean, value: false },
    showAccount: { type: Boolean, value: false },
    accountText: { type: String, value: "" },
  },

  data: {
    statusBarHeight: 20,
    navigationHeight: 44,
    totalHeight: 64,
    actionRight: 104,
  },

  lifetimes: {
    attached() {
      let windowInfo = {};
      try {
        windowInfo = typeof wx.getWindowInfo === "function" ? wx.getWindowInfo() : wx.getSystemInfoSync();
      } catch (error) {
        windowInfo = { statusBarHeight: 20, windowWidth: 375 };
      }

      let menu = null;
      try {
        menu = wx.getMenuButtonBoundingClientRect();
      } catch (error) {
        menu = null;
      }

      const statusBarHeight = windowInfo.statusBarHeight || 20;
      const navigationHeight = menu
        ? Math.max(44, (menu.top - statusBarHeight) * 2 + menu.height)
        : 44;
      const actionRight = menu
        ? Math.max(92, (windowInfo.windowWidth || 375) - menu.left + 8)
        : 104;

      this.setData({
        statusBarHeight,
        navigationHeight,
        totalHeight: statusBarHeight + navigationHeight,
        actionRight,
      });
    },
  },

  methods: {
    goBack() {
      wx.navigateBack({
        fail: () => wx.switchTab({ url: "/pages/home/index" }),
      });
    },

    openAccount() {
      this.triggerEvent("account");
    },
  },
});
