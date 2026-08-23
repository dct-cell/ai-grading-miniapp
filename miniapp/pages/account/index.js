const app = getApp();

Page({
  data: {
    publicId: "",
    avatarText: "我",
    error: "",
  },

  async onShow() {
    try {
      const user = await app.whenReady();
      this.setData({
        // Only the public id is displayed. The raw session token is never
        // rendered and never logged.
        publicId: (user && user.public_id) || "",
        avatarText: user && user.public_id ? String(user.public_id).slice(-1).toUpperCase() : "我",
        error: "",
      });
    } catch (error) {
      this.setData({ error: error.detail || "加载失败。" });
    }
  },

  goOrders() {
    wx.switchTab({ url: "/pages/orders/index" });
  },

  confirmLogout() {
    wx.showModal({
      title: "退出登录",
      content: "退出后需要重新登录才能查看订单，确定退出？",
      success: result => {
        if (!result.confirm) {
          return;
        }
        app.auth.logout();
        wx.reLaunch({ url: "/pages/home/index" });
      },
    });
  },
});
