# 安卓 APK（网页套壳）

**iOS 做不了**：编译要 Mac + Xcode，安装要 Apple 开发者账号（$99/年，免费账号签名 7 天就过期）。
iOS 用「添加到主屏幕」的 PWA 就行，体验已经和 App 一样。

安卓用 **PWABuilder**（微软的免费工具，云端构建）——**不用在本机装 Android SDK**。

## 一、为什么套壳的价值其实有限

PWA 已经能做到：全屏、自己的图标、系统通知（Web Push 已上线）、离线缓存、
网站更新后 App 自动跟着更新（不用重新发版）。

套壳 APK 只多一样：**安装方式更熟悉**（点 APK 装，而不是去浏览器菜单里找「添加到主屏幕」）。

所以先问一句：朋友会不会自己点「添加到主屏幕」？
- 会 → **不用做 APK**
- 不会 → 做，下面就是步骤

## 二、打包步骤（在浏览器里，约 10 分钟）

1. 打开 <https://www.pwabuilder.com>，输入 `https://giftia.icu`，点 Start
2. 它会检查一遍 PWA 合规性。**必须全绿**，特别这三项：
   - manifest 有效（本项目已就绪：`start_url` / `scope` / `display` / 192+512 图标都有）
   - 有 service worker（`web/sw.js` 已注册）
   - HTTPS（Cloudflare 提供）
3. 点 **Package for stores** → 选 **Android**
4. 填：
   - **App name**：`艾拉`
   - **Package ID**：`icu.giftia.aila`（任意反域名，装了以后不能改）
   - **Signing key**：选 **Create new**（让 PWABuilder 生成）
     ⚠️ **一定要把它的签名密钥下载下来存好** —— 丢了以后就不能给这个 App 发更新了
   - 选 **Trusted Web Activity**（无地址栏，最像原生）
5. 下载 ZIP，里面有关键的两样：
   - `*.apk` / `*.aab` —— 安装包
   - `assetlinks.json` —— **必须放到网站上**，否则 TWA 顶上会冒出地址栏
6. 把 `assetlinks.json` 的内容覆盖到本项目的 `web/.well-known/assetlinks.json`
   （现在是占位的 `[]`），然后 `deploy/setup.sh` 重新部署
7. 验证：
   ```
   curl https://giftia.icu/.well-known/assetlinks.json
   ```
   应该返回带 `sha256_cert_fingerprints` 的 JSON
8. 把 APK 发给朋友：他们点开 → 允许「安装未知来源的应用」→ 装上

## 三、要注意的

| 事项 | 说明 |
|---|---|
| **不上架应用商店** | 国内上架要资质；Google Play 国内用不了。直接发 APK（侧载）。 |
| **APK 里不含口令** | 所以安装包被转发也没关系（除非把口令一起转） |
| **朋友可能不敢装** | 国内安卓用户被教育过"装未知来源有风险"，有人会犹豫 |
| **签名密钥要存好** | 丢了不能发更新，只能换包名重发（等于重装） |
| **每次更新网站，App 自动跟着更新** | 因为它是加载网址的，不是打包进去的 |

## 四、不做 APK 的替代方案

手机上打开 `https://giftia.icu` → 浏览器菜单 → **添加到主屏幕**。
效果是：独立图标、全屏运行、能收通知。**和 APK 几乎没区别。**
