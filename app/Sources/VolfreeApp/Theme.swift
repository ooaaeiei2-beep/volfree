import AppKit
import SwiftUI

/// 三态语义色。取值直接来自 design/popover.html 的 CSS 变量，
/// 保证原型与实现是同一套数值，而不是"照着感觉调"。
enum Tone: Equatable {
    case safe
    case caution
    case danger

    /// 用于「多块盘时先看哪块」：越危险越靠前。
    var severity: Int {
        switch self {
        case .safe: return 0
        case .caution: return 1
        case .danger: return 2
        }
    }

    struct Palette {
        let foreground: Color
        let background: Color
        let solid: Color
    }

    func palette(for scheme: ColorScheme) -> Palette {
        let dark = scheme == .dark
        switch self {
        case .safe:
            return dark
                ? Palette(foreground: rgb(0x30, 0xD1, 0x58), background: rgba(48, 209, 88, 0.13),
                          solid: rgb(0x30, 0xD1, 0x58))
                : Palette(foreground: rgb(0x1B, 0x6B, 0x2F), background: rgba(52, 199, 89, 0.11),
                          solid: rgb(0x1B, 0x6B, 0x2F))
        case .caution:
            return dark
                ? Palette(foreground: rgb(0xFF, 0xC5, 0x3D), background: rgba(255, 159, 10, 0.15),
                          solid: rgb(0xFF, 0xC5, 0x3D))
                : Palette(foreground: rgb(0x7D, 0x49, 0x00), background: rgba(255, 159, 10, 0.13),
                          solid: rgb(0x7D, 0x49, 0x00))
        case .danger:
            return dark
                ? Palette(foreground: rgb(0xFF, 0x7B, 0x73), background: rgba(255, 69, 58, 0.14),
                          solid: rgb(0xCF, 0x35, 0x2B))
                : Palette(foreground: rgb(0xB0, 0x2A, 0x22), background: rgba(255, 59, 48, 0.10),
                          solid: rgb(0xD3, 0x32, 0x2A))
        }
    }

    /// 菜单栏图标颜色，**按给定的外观解析成具体颜色**。
    ///
    /// 这里原来返回的是动态 `NSColor`（`NSColor(name:handler:)`），交给
    /// `button.contentTintColor`。那条路走不通，实测两次：
    ///
    /// 1. 动态色在**赋值那一刻**就按 `NSApp` 的外观解析了，之后不再重算。
    /// 2. 系统的浅色模式 + 深色壁纸时，`NSApp.effectiveAppearance` 是 `Aqua`，
    ///    而状态栏按钮的 `effectiveAppearance` 是 `VibrantDark`。于是同一个色
    ///    按 NSApp 解析成深绿 `(15,86,33)`，按按钮解析是白色——前者正好沉进
    ///    深色菜单栏，用户看到的就是"黑的"。
    ///
    /// 所以现在不交给 AppKit 去解析：由 `StatusIcon` 自己按按钮外观取色、自己画。
    func statusBarColor(for appearance: NSAppearance) -> NSColor {
        let dark = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        switch self {
        case .safe:
            // 平常就是黑白，和系统菜单栏图标一致。
            // 「没事」这件事不需要用颜色强调，彩色图标在一排系统图标里反而突兀。
            return dark ? NSColor(white: 1.00, alpha: 1)
                        : NSColor(white: 0.12, alpha: 1)
        case .caution:
            // 只有真的要提醒时才上色——黄/红在深色菜单栏上必须够亮才看得见。
            return dark ? NSColor(srgbRed: 1.00, green: 0.80, blue: 0.29, alpha: 1)
                        : NSColor(srgbRed: 0.42, green: 0.25, blue: 0.00, alpha: 1)
        case .danger:
            return dark ? NSColor(srgbRed: 1.00, green: 0.52, blue: 0.48, alpha: 1)
                        : NSColor(srgbRed: 0.62, green: 0.13, blue: 0.10, alpha: 1)
        }
    }

    private func rgb(_ r: Int, _ g: Int, _ b: Int) -> Color {
        Color(.sRGB, red: Double(r) / 255, green: Double(g) / 255, blue: Double(b) / 255, opacity: 1)
    }

    private func rgba(_ r: Int, _ g: Int, _ b: Int, _ a: Double) -> Color {
        Color(.sRGB, red: Double(r) / 255, green: Double(g) / 255, blue: Double(b) / 255, opacity: a)
    }
}

/// 菜单栏图标：**两条路分开用**。
///
/// - **常态（safe）走 template 图**，颜色完全交给系统。明暗、增强对比度、
///   降低透明度这些都由 AppKit 处理，我们一行都不用管，也不用监听换肤。
/// - **黄 / 红用调色板（`paletteColors`）上色**。符号仍由 AppKit 自己渲染，
///   只是把颜色换掉——所以两条路的几何完全一致，切换告警态时图标不会变胖变瘦。
///
/// 为什么不统一用 `contentTintColor` 给 template 图上色（这是很多人第一反应）：
/// 实测在 `NSStatusBarButton` 上它**不管给什么颜色都画成纯黑**。三种都试过——
/// 动态 `NSColor`、纯白、琥珀黄，`cacheDisplay` 抓出来的主色全是 `(0,0,0)`，
/// 与用户截图里那个纯黑图标完全一致。所以那条路是死的，不是参数没调好。
enum StatusIcon {

    /// 角标圆点直径 / 图标高度。0.30 在 16px 下是 5px——再小就看不清，再大会咬掉盖子一角。
    private static let badgeRatio: CGFloat = 0.30
    /// 圆点与字形之间那道透明缝隙的宽度（点）。
    private static let badgeRing: CGFloat = 0.9

    private static func symbol(pointSize: CGFloat,
                               palette: [NSColor]? = nil) -> NSImage? {
        var config = NSImage.SymbolConfiguration(pointSize: pointSize, weight: .regular)
        if let palette {
            config = config.applying(NSImage.SymbolConfiguration(paletteColors: palette))
        }
        return NSImage(systemSymbolName: "externaldrive.fill",
                       accessibilityDescription: "volfree")?
            .withSymbolConfiguration(config)
    }

    /// 常态：template 图，不染色。
    ///
    /// `isTemplate = true` 的图会被系统当成蒙版，用菜单栏当前的前景色填充——
    /// 深色菜单栏上就是白的，浅色上就是黑的，且会跟随「增强对比度」等设置。
    static func template(pointSize: CGFloat = 15) -> NSImage {
        guard let image = symbol(pointSize: pointSize) else {
            return NSImage(size: NSSize(width: pointSize, height: pointSize))
        }
        image.isTemplate = true
        return image
    }

    /// 黄 / 红：硬盘保持黑白（与常态同一个字形），右上角加一个彩色小圆点。
    ///
    /// 为什么不整盘染色：`externaldrive.fill` 的盖子与机身之间是一条**透明缝隙**，
    /// 两边都染上色之后这条缝隙就退化成一根细线，16px 下整个图标糊成一个色块，
    /// 硬盘的形状信息全丢了——用户说的"丑"就是这个机制，不是色值没调好。
    /// 保留黑白机身、只加一个点，形状和信号都还在。
    ///
    /// 底图仍走 `paletteColors`：几何与 template 版逐像素一致，切换时硬盘不会变胖变瘦。
    /// 只有右上角那一小块是被有意改动的（挖环 + 点）。
    static func image(for tone: Tone, appearance: NSAppearance,
                      pointSize: CGFloat = 15) -> NSImage {
        guard tone != .safe else { return template(pointSize: pointSize) }

        // 常态色就是"前景色"（深色菜单栏白、浅色黑），直接复用，避免两处各写一遍
        let foreground = Tone.safe.statusBarColor(for: appearance)
        let accent = tone.statusBarColor(for: appearance)
        guard let body = symbol(pointSize: pointSize, palette: [foreground]) else {
            return template(pointSize: pointSize)
        }

        let size = body.size
        let diameter = max(3, (size.height * badgeRatio).rounded())
        let center = NSPoint(x: size.width - diameter / 2 - 0.2,
                             y: size.height - diameter / 2 - 0.2)

        let image = NSImage(size: size, flipped: false) { rect in
            body.draw(in: rect)

            // 先挖一圈透明，让圆点与字形分开。
            // 挖出来的缝隙透出的是菜单栏本身，所以在深浅背景上都成立，
            // 不需要知道背景到底是什么颜色。
            NSGraphicsContext.current?.compositingOperation = .destinationOut
            NSBezierPath(ovalIn: NSRect(x: center.x - diameter/2 - badgeRing,
                                        y: center.y - diameter/2 - badgeRing,
                                        width: diameter + badgeRing*2,
                                        height: diameter + badgeRing*2)).fill()
            NSGraphicsContext.current?.compositingOperation = .sourceOver

            accent.set()
            NSBezierPath(ovalIn: NSRect(x: center.x - diameter/2, y: center.y - diameter/2,
                                        width: diameter, height: diameter)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }
}

enum Metrics {
    /// 面板宽度。原型与实现共用同一个数字，改宽度只需改这里。
    static let panelWidth: CGFloat = 340
    static let panelPadding: CGFloat = 12
    static let cornerRadius: CGFloat = 14
    static let primaryHeight: CGFloat = 30
    static let rowHeight: CGFloat = 32
    /// 区块之间（卡片↔按钮↔横线↔底栏）的呼吸位。
    /// 原型里这几处都是 11pt，实现必须一致，否则按钮会贴到横线上。
    static let sectionGap: CGFloat = 11

    static func accent(_ scheme: ColorScheme) -> Color {
        scheme == .dark
            ? Color(.sRGB, red: 0x0A / 255, green: 0x70 / 255, blue: 0xDA / 255, opacity: 1)
            : Color(.sRGB, red: 0x00 / 255, green: 0x71 / 255, blue: 0xE3 / 255, opacity: 1)
    }

    static func hairline(_ scheme: ColorScheme) -> Color {
        scheme == .dark
            ? Color.white.opacity(0.10)
            : Color.black.opacity(0.09)
    }

    static func subtleFill(_ scheme: ColorScheme) -> Color {
        scheme == .dark
            ? Color.white.opacity(0.09)
            : Color.black.opacity(0.055)
    }
}
