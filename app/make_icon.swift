// volfree 的 app 图标生成器。
//
// 设计来源就是面板头部那个 30pt 小图标（`PopoverView.swift` 的 `header`）：
// 蓝色对角渐变底 + 白色 `externaldrive.fill`。用户看过实物之后说"用这个不就好了"，
// 所以这里不做任何再创作，只把它按 app 图标的比例放大。
//
// 为什么用 Swift 而不是 Python 画：
// 符号是 SF Symbol，手绘复刻必然是近似；用 AppKit 渲染能保证与面板里那个
// 逐像素同源。代价是构建时多一步 swiftc，但只在出图标时需要，不影响 app 构建。
//
// 比例（从 692×354 的实机截图上量出来的，底板 60×60）：
//   符号宽 48.3% 底板宽、高 36.7% 底板高、中心偏移 x -0.5 / y 0
//
// 用法：
//   make_icon <输出目录>
// 产出 <输出目录>/AppIcon.iconset/ 下 10 个 PNG，交给 iconutil 打包。

import AppKit
import Foundation

// ---- 与面板头部一致的参数 ----
let gradientTop = NSColor(srgbRed: 0x78 / 255, green: 0x97 / 255, blue: 0xBD / 255, alpha: 1)
let gradientBottom = NSColor(srgbRed: 0x48 / 255, green: 0x68 / 255, blue: 0x8F / 255, alpha: 1)
let symbolName = "externaldrive.fill"
let symbolOpacity: CGFloat = 0.95
let symbolWidthRatio: CGFloat = 0.483   // 符号宽 / 底板宽

let canvas = 1024.0                     // 逻辑画布
let bodyRatio = 824.0 / 1024.0          // 超椭圆占画布的比例（macOS 图标惯例）
let squircleN: CGFloat = 5.0

/// 小尺寸要把符号额外放大一点。
///
/// 16px 下底板只有 12.9px 宽，按 48.3% 算符号只有 6.2px——上盖分隔线和指示灯
/// 会一起糊掉，整个符号读起来只是一个白块。放大会牺牲一点留白，但小尺寸下
/// "认得出是什么"比"留白比例精确"重要得多。
func symbolRatio(for size: Int) -> CGFloat {
    switch size {
    case ..<24: return 0.66
    case ..<48: return 0.56
    default: return symbolWidthRatio
    }
}

/// 超椭圆路径。n=5 接近 Apple 的 squircle，纯圆角矩形会显得"胖"。
func squirclePath(in rect: NSRect, n: CGFloat = squircleN) -> NSBezierPath {
    let path = NSBezierPath()
    let cx = rect.midX, cy = rect.midY
    let a = rect.width / 2, b = rect.height / 2
    let steps = 1440
    for i in 0...steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let ct = cos(t), st = sin(t)
        // 参数方程：|x|^n + |y|^n = 1 等价于 x = |cos t|^(2/n) * sgn(cos t)
        let x = cx + a * pow(abs(ct), 2 / n) * (ct < 0 ? -1 : 1)
        let y = cy + b * pow(abs(st), 2 / n) * (st < 0 ? -1 : 1)
        if i == 0 { path.move(to: NSPoint(x: x, y: y)) } else { path.line(to: NSPoint(x: x, y: y)) }
    }
    path.close()
    return path
}

/// 白色（可带透明度）的符号图。SF Symbol 是模板图，需要自己上色。
func tintedSymbol(pointSize: CGFloat) -> NSImage? {
    let cfg = NSImage.SymbolConfiguration(pointSize: pointSize, weight: .medium)
    guard let base = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)?
        .withSymbolConfiguration(cfg) else { return nil }

    let size = base.size
    let out = NSImage(size: size, flipped: false) { rect in
        base.draw(in: rect)
        NSColor.white.withAlphaComponent(symbolOpacity).set()
        rect.fill(using: .sourceAtop)
        return true
    }
    return out
}

/// 量出符号的**实际墨迹**边界，坐标是 `image` 自己的逻辑坐标系（原点左下）。
///
/// 不能直接用 `image.size` 缩放：SF Symbol 的 image 尺寸含四周留白，
/// 按它算出来的符号会明显偏小（实测只到底板宽的 39.4%，目标是 48.3%）。
/// 也不能直接居中：留白左右不一定对称，按 image 中心对齐会让符号偏一点。
func inkBounds(_ image: NSImage) -> NSRect? {
    let box = 1024
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: box, pixelsHigh: box,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ) else { return nil }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    // 按原始尺寸画在左下角（不能 draw(in:) 撑满，那样会被缩放，量出来不是原尺寸）
    image.draw(in: NSRect(origin: .zero, size: image.size))
    NSGraphicsContext.restoreGraphicsState()

    guard let data = rep.bitmapData else { return nil }
    let bpr = rep.bytesPerRow
    let spp = rep.samplesPerPixel
    var minX = box, maxX = -1, minY = box, maxY = -1
    for y in 0..<box {
        let row = y * bpr
        for x in 0..<box where data[row + x * spp + 3] > 24 {
            if x < minX { minX = x }
            if x > maxX { maxX = x }
            if y < minY { minY = y }
            if y > maxY { maxY = y }
        }
    }
    guard maxX >= minX, maxY >= minY else { return nil }

    // bitmap 的行 0 在顶部，绘图坐标原点在左下，y 要翻过来
    return NSRect(x: CGFloat(minX), y: CGFloat(box - 1 - maxY),
                  width: CGFloat(maxX - minX + 1), height: CGFloat(maxY - minY + 1))
}

/// 出某个像素尺寸的图。内部按 ss 倍超采样再缩，边缘靠缩放抗锯齿。
func render(size: Int) -> NSBitmapImageRep? {
    // 小尺寸超采样 4 倍，大尺寸 2 倍就够（本身分辨率已高）
    let ss = size <= 512 ? 4 : 2
    let px = size * ss
    let W = CGFloat(px)

    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ) else { return nil }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSGraphicsContext.current?.imageInterpolation = .high

    // 底板：居中，占画布的 bodyRatio
    let body = W * CGFloat(bodyRatio)
    let inset = (W - body) / 2
    let bodyRect = NSRect(x: inset, y: inset, width: body, height: body)
    let path = squirclePath(in: bodyRect)

    NSGradient(starting: gradientTop, ending: gradientBottom)?.draw(in: path, angle: -45)

    // 符号：按"墨迹宽占底板 48.3%"缩放，并把**墨迹中心**对齐到画布中心。
    // 这两个都用墨迹量，不用 image.size——后者的四周留白会把符号缩偏、也带一点偏移。
    let targetW = body * symbolRatio(for: size)
    if let probe = tintedSymbol(pointSize: 400), let ink = inkBounds(probe) {
        let scale = targetW / ink.width
        let w = probe.size.width * scale
        let h = probe.size.height * scale
        let x = (W - w) / 2 + (probe.size.width / 2 - ink.midX) * scale
        let y = (W - h) / 2 + (probe.size.height / 2 - ink.midY) * scale
        probe.draw(in: NSRect(x: x, y: y, width: w, height: h))
    }

    NSGraphicsContext.restoreGraphicsState()

    if ss == 1 { return rep }
    // 缩回目标尺寸
    guard let out = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ) else { return nil }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: out)
    NSGraphicsContext.current?.imageInterpolation = .high
    let hi = NSImage(size: NSSize(width: px, height: px))
    hi.addRepresentation(rep)
    hi.draw(in: NSRect(x: 0, y: 0, width: size, height: size))
    NSGraphicsContext.restoreGraphicsState()
    return out
}

func write(_ rep: NSBitmapImageRep, to path: String) throws {
    guard let data = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "make_icon", code: 1,
                      userInfo: [NSLocalizedDescriptionKey: "PNG 编码失败: \(path)"])
    }
    try data.write(to: URL(fileURLWithPath: path))
}

// ---- 主流程 ----
let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("用法: make_icon <输出目录>\n".data(using: .utf8)!)
    exit(2)
}
let outDir = args[1]
let iconset = (outDir as NSString).appendingPathComponent("AppIcon.iconset")
try? FileManager.default.removeItem(atPath: iconset)
try FileManager.default.createDirectory(atPath: iconset, withIntermediateDirectories: true)

// 先出 1024 母版（也作为 512@2x）
let master = render(size: 1024)!
try write(master, to: (outDir as NSString).appendingPathComponent("icon-1024.png"))
print("写出 \(outDir)/icon-1024.png")

for base in [16, 32, 128, 256, 512] {
    for (scale, suffix) in [(1, ""), (2, "@2x")] {
        let px = base * scale
        let rep = render(size: px)!
        let name = "icon_\(base)x\(base)\(suffix).png"
        try write(rep, to: (iconset as NSString).appendingPathComponent(name))
    }
}
print("写出 \(iconset) 下 10 个尺寸")
