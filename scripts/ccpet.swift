// ccpet.swift — Independent Claude Code desktop pet
//
// A standalone macOS mascot that renders Codex pet spritesheets in its own
// transparent, always-on-top, non-activating floating window — independent of
// Codex. Driven by the Claude Code hook bridge over a Unix socket.
//
// Sprite contract (verified from the Codex app):
//   atlas = 8 columns x 9 rows, each cell 192x208px (tight grid, alpha webp).
//   rows (0..8): idle, running-right, running-left, waving, jumping, failed,
//                waiting, running, review.
//   per-row frame counts and per-frame durations replicated below.
//
// Build (done by the bridge on first run):
//   swiftc -O ccpet.swift -o ccpet -framework AppKit
//
// Run:
//   ccpet --selftest            # cycle through states for visual QA
//   ccpet                       # daemon: listen on $TMPDIR/ccpet/daemon.sock
//
// Wire protocol (newline-delimited JSON, one object per line):
//   {"type":"state","session":"<id>","state":"thinking|running|waiting|failed|review|idle",
//    "text":"...","title":"...","open_cmd":"...","surface":"..."}
//   {"type":"switch_pet","pet":"<name>"}
//   {"type":"ping"}   -> replies {"type":"pong"}

import AppKit
import Foundation

// ── Sprite contract ─────────────────────────────────────────────────────────

let CELL_W = 192
let CELL_H = 208
let COLS = 8
let ROWS = 9

// Row index per state name (row order in the atlas).
enum Row: Int {
    case idle = 0, runningRight = 1, runningLeft = 2, waving = 3, jumping = 4,
         failed = 5, waiting = 6, running = 7, review = 8
}

// Frames per row (unused columns up to 8 are blank).
let ROW_FRAME_COUNTS: [Row: Int] = [
    .idle: 6, .runningRight: 8, .runningLeft: 8, .waving: 4, .jumping: 5,
    .failed: 8, .waiting: 6, .running: 6, .review: 6,
]

// Per-frame durations (ms). normal for all frames, last = final-frame hold.
struct Timing { let normal: Double; let last: Double }
let ROW_TIMING: [Row: Timing] = [
    .runningRight: Timing(normal: 120, last: 220),
    .runningLeft:  Timing(normal: 120, last: 220),
    .waving:       Timing(normal: 140, last: 280),
    .jumping:      Timing(normal: 140, last: 280),
    .failed:       Timing(normal: 140, last: 240),
    .waiting:      Timing(normal: 150, last: 260),
    .running:      Timing(normal: 120, last: 220),
    .review:       Timing(normal: 150, last: 280),
]
// idle has a hand-tuned per-frame duration array.
let IDLE_DURATIONS: [Double] = [280, 110, 110, 140, 140, 320]

// Logical mascot state → primary row.
func rowFor(state: String) -> Row {
    switch state {
    case "running", "thinking", "tool": return .running
    case "waiting", "attention":        return .waiting
    case "failed", "error":             return .failed
    case "canceled":                    return .idle   // stopped/interrupted → settle
    case "review", "reply":             return .review
    case "jumping":                     return .jumping
    case "waving":                      return .waving
    case "running-left":                return .runningLeft
    case "running-right":               return .runningRight
    default:                             return .idle
    }
}

// Aggregate priority when multiple sessions are active (higher wins).
// Aggregate priority when multiple sessions are active (higher wins).
// idle < canceled < running(blue) < review(green) < attention(brown) < failed(red)
func priority(_ state: String) -> Int {
    switch state {
    case "failed", "error":                       return 5   // red
    case "attention", "waiting":                   return 4   // brown (needs input/approval)
    case "review", "reply":                        return 3   // green
    case "running", "thinking", "tool":            return 2   // blue
    case "canceled":                               return 1   // gray (stopped/interrupted)
    default:                                       return 0   // idle
    }
}

// ── Paths & config ──────────────────────────────────────────────────────────

let HOME = FileManager.default.homeDirectoryForCurrentUser.path
// Writable runtime dir (config + durable state) — matches the Python scripts'
// RUNTIME_DIR = ~/.ccpet. The daemon binary itself is compiled here too.
let RUNTIME_DIR = "\(HOME)/.ccpet"
let CONFIG_PATH = "\(RUNTIME_DIR)/config.json"
let PETS_DIR = "\(HOME)/.codex/pets"

func tmpDir() -> String {
    let t = ProcessInfo.processInfo.environment["TMPDIR"] ?? "/tmp"
    return t.hasSuffix("/") ? String(t.dropLast()) : t
}
func socketPath() -> String { "\(tmpDir())/ccpet/daemon.sock" }

func loadConfig() -> [String: Any] {
    guard let data = FileManager.default.contents(atPath: CONFIG_PATH),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return [:] }
    return obj
}

func currentPetName() -> String {
    let cfg = loadConfig()
    if let p = cfg["pet"] as? String, !p.isEmpty { return p }
    return "spongebob-star"
}

func spritesheetPath(pet: String) -> String { "\(PETS_DIR)/\(pet)/spritesheet.webp" }

// ── Sprite frame cache ──────────────────────────────────────────────────────

final class Sprite {
    private(set) var frames: [[CGImage]] = []   // frames[row][col]
    private(set) var loaded = false

    func load(pet: String) {
        let path = spritesheetPath(pet: pet)
        guard let img = NSImage(contentsOfFile: path),
              let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil)
        else { loaded = false; return }
        var grid: [[CGImage]] = []
        for r in 0..<ROWS {
            var rowFrames: [CGImage] = []
            for c in 0..<COLS {
                let rect = CGRect(x: c * CELL_W, y: r * CELL_H, width: CELL_W, height: CELL_H)
                if let f = cg.cropping(to: rect) { rowFrames.append(f) }
            }
            grid.append(rowFrames)
        }
        frames = grid
        loaded = true
    }

    func frame(row: Row, col: Int) -> CGImage? {
        let r = row.rawValue
        guard r < frames.count, col < frames[r].count else { return nil }
        return frames[r][col]
    }
}

// ── Animation controller ────────────────────────────────────────────────────

final class Animator {
    private let layer: CALayer
    private let sprite: Sprite
    private var timer: DispatchSourceTimer?
    private var playlist: [(row: Row, col: Int, dur: Double)] = []
    private var idx = 0
    private var loopStart = 0
    private(set) var currentState = "idle"   // base (activity) state
    private var transient: String?           // drag/hover override

    init(layer: CALayer, sprite: Sprite) {
        self.layer = layer
        self.sprite = sprite
    }

    // Direct-row rows that loop continuously while active (drag directions).
    private func loopingRow(for state: String) -> Row? {
        switch state {
        case "running-left": return .runningLeft
        case "running-right": return .runningRight
        default: return nil
        }
    }

    // Build a "play row 3x, then fall into slow idle loop" playlist (Codex-faithful).
    private func buildPlaylist(for state: String) -> ([(Row, Int, Double)], Int) {
        let row = rowFor(state: state)
        var seq: [(Row, Int, Double)] = []
        if row == .idle {
            for (c, d) in IDLE_DURATIONS.enumerated() { seq.append((.idle, c, d)) }
            return (seq, 0)   // idle loops forever from 0
        }
        let count = ROW_FRAME_COUNTS[row] ?? 1
        let t = ROW_TIMING[row] ?? Timing(normal: 125, last: 200)
        var oneCycle: [(Row, Int, Double)] = []
        for c in 0..<count {
            oneCycle.append((row, c, c == count - 1 ? t.last : t.normal))
        }
        // 3 cycles of the active row...
        for _ in 0..<3 { seq.append(contentsOf: oneCycle) }
        let loopAt = seq.count
        // ...then slow idle (idle durations x6) forever.
        for (c, d) in IDLE_DURATIONS.enumerated() { seq.append((.idle, c, d * 6)) }
        return (seq, loopAt)
    }

    func play(state: String) {
        currentState = state
        if transient == nil { rebuild(for: state) }
    }

    // Transient override (hover→jumping, drag→running-left/right). Precedence:
    // drag/hover transient > base activity state.
    func setTransient(_ state: String?) {
        transient = state
        rebuild(for: state ?? currentState)
    }

    private func rebuild(for state: String) {
        // Drag-direction rows loop continuously (not the 3x-then-idle pattern).
        if let row = loopingRow(for: state) {
            let count = ROW_FRAME_COUNTS[row] ?? 1
            let t = ROW_TIMING[row] ?? Timing(normal: 125, last: 200)
            var seq: [(Row, Int, Double)] = []
            for c in 0..<count { seq.append((row, c, c == count - 1 ? t.last : t.normal)) }
            playlist = seq
            loopStart = 0
            idx = 0
            scheduleCurrent()
            return
        }
        let (list, loopAt) = buildPlaylist(for: state)
        playlist = list
        loopStart = loopAt
        idx = 0
        scheduleCurrent()
    }

    func reloadSprite() { showFrame() }

    private func showFrame() {
        guard idx < playlist.count else { return }
        let (row, col, _) = playlist[idx]
        if let f = sprite.frame(row: row, col: col) {
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            layer.contents = f
            CATransaction.commit()
        }
    }

    private func scheduleCurrent() {
        timer?.cancel()
        guard idx < playlist.count else { return }
        showFrame()
        let dur = playlist[idx].dur
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + .milliseconds(Int(dur)))
        t.setEventHandler { [weak self] in self?.advance() }
        timer = t
        t.resume()
    }

    private func advance() {
        idx += 1
        if idx >= playlist.count { idx = loopStart }
        scheduleCurrent()
    }
}

// ── Pet window ──────────────────────────────────────────────────────────────

// A view that distinguishes a click from a drag (4px threshold) and lets the
// window be dragged by its body.
final class PetView: NSView {
    var onClick: (() -> Void)?
    var onDragSprite: ((String?) -> Void)?   // "running-left"/"running-right"/nil
    var onHover: ((Bool) -> Void)?
    var onMoved: (() -> Void)?               // window moved (drag) → relayout cards
    private var downAt: NSPoint = .zero
    private var dragging = false
    private var lastDragRow: String?
    private var tracking: NSTrackingArea?

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                               owner: self, userInfo: nil)
        addTrackingArea(t)
        tracking = t
    }

    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }

    override func mouseDown(with event: NSEvent) {
        downAt = event.locationInWindow
        dragging = false
        lastDragRow = nil
    }

    override func mouseDragged(with event: NSEvent) {
        let p = event.locationInWindow
        let dx = p.x - downAt.x, dy = p.y - downAt.y
        if !dragging && (dx * dx + dy * dy) > 16 { dragging = true }  // 4px threshold
        guard dragging, let win = window else { return }
        // Direction sprite: deltaX ≥ +4 → running-right, ≤ −4 → running-left,
        // |Δ|<4 → keep previous (hysteresis).
        if event.deltaX >= 4 {
            if lastDragRow != "running-right" { lastDragRow = "running-right"; onDragSprite?("running-right") }
        } else if event.deltaX <= -4 {
            if lastDragRow != "running-left" { lastDragRow = "running-left"; onDragSprite?("running-left") }
        }
        var origin = win.frame.origin
        origin.x += event.deltaX
        origin.y -= event.deltaY
        win.setFrameOrigin(origin)
        onMoved?()   // keep notification cards anchored to the pet
    }

    override func mouseUp(with event: NSEvent) {
        if dragging {
            onDragSprite?(nil)   // clear drag transient
        } else {
            onClick?()
        }
        dragging = false
        lastDragRow = nil
    }
}

// Small bottom-right resize handle, shown on hover; drag to resize the pet.
final class ResizeHandle: NSView {
    var onResizeStart: (() -> Void)?
    var onResizeTo: ((CGFloat) -> Void)?   // absolute cursor screen-X
    var onResizeEnd: (() -> Void)?
    var visible = false { didSet { needsDisplay = true } }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .crosshair)
    }
    override func draw(_ dirtyRect: NSRect) {
        guard visible else { return }
        // Codex-style diagonal double-arrow, bottom-right corner.
        let c = NSColor(white: 0.45, alpha: 0.95)
        c.setStroke()
        let b = bounds.insetBy(dx: 4, dy: 4)
        let p = NSBezierPath(); p.lineWidth = 1.6; p.lineCapStyle = .round; p.lineJoinStyle = .round
        // main diagonal
        p.move(to: NSPoint(x: b.minX, y: b.maxY))
        p.line(to: NSPoint(x: b.maxX, y: b.minY))
        // arrowhead at bottom-right
        p.move(to: NSPoint(x: b.maxX - 5, y: b.minY))
        p.line(to: NSPoint(x: b.maxX, y: b.minY))
        p.line(to: NSPoint(x: b.maxX, y: b.minY + 5))
        // arrowhead at top-left
        p.move(to: NSPoint(x: b.minX + 5, y: b.maxY))
        p.line(to: NSPoint(x: b.minX, y: b.maxY))
        p.line(to: NSPoint(x: b.minX, y: b.maxY - 5))
        p.stroke()
    }
    override func mouseDown(with event: NSEvent) { onResizeStart?() }
    override func mouseDragged(with event: NSEvent) {
        onResizeTo?(NSEvent.mouseLocation.x)   // absolute screen X, drift-free
    }
    override func mouseUp(with event: NSEvent) { onResizeEnd?() }
}

final class PetWindow: NSObject {
    let panel: NSPanel
    let sprite = Sprite()
    let animator: Animator
    var onClick: (() -> Void)?
    var onMoved: (() -> Void)?
    var onToggleCollapse: (() -> Void)?
    private let contentLayer = CALayer()
    private let handle = ResizeHandle()
    private let collapseBtn = NSButton()
    private let aspect = CGFloat(CELL_W) / CGFloat(CELL_H)   // 192/208
    // Width in points. Default 96, clamp 64..192 (≈ spec's 112px/80..224 at 2x).
    private var widthPt: CGFloat = 96
    private let minW: CGFloat = 64, maxW: CGFloat = 192
    private var resizeStartW: CGFloat = 0
    private var resizeStartCursorX: CGFloat = 0
    private var resizeAnchorRight: CGFloat = 0
    private var resizeAnchorBottom: CGFloat = 0

    override init() {
        let saved = UserDefaults.standard.double(forKey: "ccpet-mascot-width-pt")
        if saved >= 64 && saved <= 192 { widthPt = CGFloat(saved) }
        let w = widthPt
        let h = w / aspect
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: w, height: h),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false)
        animator = Animator(layer: contentLayer, sprite: sprite)
        super.init()

        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = false

        let view = PetView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        view.wantsLayer = true
        view.onClick = { [weak self] in self?.onClick?() }
        view.onDragSprite = { [weak self] dir in self?.setDragSprite(dir) }
        view.onHover = { [weak self] h in self?.setHover(h) }
        view.onMoved = { [weak self] in self?.onMoved?() }
        view.autoresizingMask = [.width, .height]
        contentLayer.frame = view.bounds
        contentLayer.contentsGravity = .resizeAspect
        contentLayer.magnificationFilter = .nearest   // pixelated
        view.layer?.addSublayer(contentLayer)

        // Resize handle (bottom-right) + collapse button (top-right). Their frames
        // are set by layoutChrome() so they scale with the pet and never drift; no
        // autoresizingMask (which would keep a fixed size at a shifting position).
        handle.onResizeStart = { [weak self] in self?.beginResize() }
        handle.onResizeTo = { [weak self] x in self?.resizeTo(cursorX: x) }
        handle.onResizeEnd = { [weak self] in self?.persistWidth() }
        view.addSubview(handle)

        collapseBtn.isBordered = false
        collapseBtn.wantsLayer = true
        collapseBtn.layer?.backgroundColor = NSColor(white: 0.15, alpha: 0.82).cgColor
        collapseBtn.bezelStyle = .regularSquare
        collapseBtn.attributedTitle = NSAttributedString(string: "▾", attributes: [
            .foregroundColor: NSColor.white, .font: NSFont.boldSystemFont(ofSize: 12)])
        collapseBtn.target = self
        collapseBtn.action = #selector(collapseTapped)
        collapseBtn.isHidden = true   // only shown when there are cards
        view.addSubview(collapseBtn)

        panel.contentView = view
        layoutChrome()
        sprite.load(pet: currentPetName())
        positionBottomRight()
    }

    @objc private func collapseTapped() { onToggleCollapse?() }

    // Show/hide the collapse control, set its glyph (▾ expanded, count collapsed),
    // and — when collapsed — tint the badge by the aggregate state. Font size &
    // corner radius are owned by layoutChrome() (they scale with the pet).
    func setCollapseControl(visible: Bool, collapsed: Bool, count: Int,
                            badgeColor: NSColor? = nil) {
        collapseBtn.isHidden = !visible
        let s = collapsed ? "\(count)" : "▾"
        let fontSize = collapseBtn.frame.width * 0.5
        collapseBtn.attributedTitle = NSAttributedString(string: s, attributes: [
            .foregroundColor: NSColor.white,
            .font: NSFont.boldSystemFont(ofSize: max(10, fontSize))])
        // Expanded → neutral dark chip; collapsed → colored by state.
        let bg = (collapsed ? badgeColor : nil) ?? NSColor(white: 0.15, alpha: 0.82)
        collapseBtn.layer?.backgroundColor = bg.cgColor
    }

    // Lay out the resize handle + collapse button so they keep a constant size
    // *relative to the pet* at any zoom. Called on init and every resize so the
    // chrome never drifts. Sizes scale with widthPt (clamped to sane bounds).
    private func layoutChrome() {
        guard let v = panel.contentView else { return }
        let w = v.bounds.width, h = v.bounds.height
        // Handle ≈ 21% of width, clamped; sits flush in the bottom-right corner.
        let hs = max(16, min(28, w * 0.21))
        handle.frame = NSRect(x: w - hs, y: 0, width: hs, height: hs)
        // Collapse button ≈ 23% of width, clamped; top-right corner.
        let cs = max(18, min(30, w * 0.23))
        collapseBtn.frame = NSRect(x: w - cs, y: h - cs, width: cs, height: cs)
        collapseBtn.layer?.cornerRadius = cs / 2
        collapseBtn.attributedTitle = NSAttributedString(
            string: collapseBtn.attributedTitle.string,
            attributes: [.foregroundColor: NSColor.white,
                         .font: NSFont.boldSystemFont(ofSize: max(10, cs * 0.5))])
    }

    private func syncLayerFrame() {
        if let v = panel.contentView {
            contentLayer.frame = v.bounds
        }
    }

    private func beginResize() {
        resizeStartW = widthPt
        resizeStartCursorX = NSEvent.mouseLocation.x
        resizeAnchorRight = panel.frame.maxX
        resizeAnchorBottom = panel.frame.minY
    }

    private func resizeTo(cursorX: CGFloat) {
        // Spec: newWidth = clamp(startW + (cursorX − startCursorX)); bottom-right
        // corner fixed. Absolute-start basis → drift-free.
        let newW = max(minW, min(maxW, resizeStartW + (cursorX - resizeStartCursorX)))
        if abs(newW - widthPt) < 0.5 { return }
        widthPt = newW
        let newH = newW / aspect
        let newOrigin = NSPoint(x: resizeAnchorRight - newW, y: resizeAnchorBottom)
        // Update panel + layer + chrome atomically with implicit animations off,
        // so no intermediate frame shows a mismatched (clipped) layer/panel.
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        panel.setFrame(NSRect(origin: newOrigin, size: NSSize(width: newW, height: newH)),
                       display: true)
        syncLayerFrame()
        layoutChrome()
        CATransaction.commit()
        // Cards anchor to the pet's frame → move them as the pet grows/shrinks.
        onMoved?()
    }

    private func persistWidth() {
        UserDefaults.standard.set(Double(widthPt), forKey: "ccpet-mascot-width-pt")
    }

    private func positionBottomRight() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        // Keep a generous margin so the pet isn't clipped by the screen edge / Dock.
        panel.setFrameOrigin(NSPoint(x: vf.maxX - size.width - 80,
                                     y: vf.minY + 120))
    }

    func show() { panel.orderFrontRegardless() }
    func hidePet() { panel.orderOut(nil) }
    func showPet() { panel.orderFrontRegardless() }

    func centerOnScreen() {
        guard let screen = NSScreen.main else { return }
        let vf = screen.visibleFrame
        let size = panel.frame.size
        panel.setFrameOrigin(NSPoint(x: vf.midX - size.width / 2,
                                     y: vf.midY - size.height / 2))
    }

    func setState(_ state: String) { animator.play(state: state) }

    // Transient precedence: drag direction > hover(jumping) > base state.
    private var hovering = false
    private var dragDir: String?
    private func applyTransient() {
        if let d = dragDir { animator.setTransient(d) }
        else if hovering { animator.setTransient("jumping") }
        else { animator.setTransient(nil) }
    }
    func setHover(_ h: Bool) { hovering = h; handle.visible = h; applyTransient() }
    func setDragSprite(_ dir: String?) { dragDir = dir; applyTransient() }

    func switchPet(_ pet: String) {
        sprite.load(pet: pet)
        animator.reloadSprite()
    }

    var frame: NSRect { panel.frame }
}

// ── Notification card (one per session; click → open that session) ──────────────

final class CardView: NSView {
    var onClick: (() -> Void)?
    var onHover: ((Bool) -> Void)?
    private var tracking: NSTrackingArea?
    override func mouseUp(with event: NSEvent) { onClick?() }
    override var isFlipped: Bool { false }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let t = tracking { removeTrackingArea(t) }
        let t = NSTrackingArea(rect: bounds,
                               options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                               owner: self, userInfo: nil)
        addTrackingArea(t); tracking = t
    }
    override func mouseEntered(with event: NSEvent) { onHover?(true) }
    override func mouseExited(with event: NSEvent) { onHover?(false) }
}

final class CardWindow: NSObject {
    let panel: NSPanel
    var onClick: (() -> Void)?
    var onClose: (() -> Void)?
    private let titleLabel = NSTextField(labelWithString: "")
    private let subtitleLabel = NSTextField(labelWithString: "")
    private let bodyLabel = NSTextField(labelWithString: "")
    private let iconView = NSTextField(labelWithString: "")
    private let spinner = NSProgressIndicator()
    private let closeBtn = NSButton()
    let cardWidth: CGFloat = 300
    private let cardHeight: CGFloat = 72

    override init() {
        panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: cardWidth, height: cardHeight),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered, defer: false)
        super.init()
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.hidesOnDeactivate = false

        // Blurred rounded background (NSVisualEffectView).
        let blur = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: cardWidth, height: cardHeight))
        blur.material = .hudWindow
        blur.state = .active
        blur.blendingMode = .behindWindow
        blur.wantsLayer = true
        blur.layer?.cornerRadius = 16
        blur.layer?.masksToBounds = true
        blur.autoresizingMask = [.width, .height]

        let card = CardView(frame: NSRect(x: 0, y: 0, width: cardWidth, height: cardHeight))
        card.wantsLayer = true
        card.onClick = { [weak self] in self?.onClick?() }
        card.onHover = { [weak self] h in self?.setHover(h) }
        card.autoresizingMask = [.width, .height]
        card.addSubview(blur)

        // Close (✕) button — top-left, revealed on hover; dismisses this card.
        closeBtn.frame = NSRect(x: 6, y: cardHeight - 24, width: 18, height: 18)
        closeBtn.bezelStyle = .circular
        closeBtn.isBordered = false
        closeBtn.title = "✕"
        closeBtn.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
        closeBtn.contentTintColor = .secondaryLabelColor
        closeBtn.wantsLayer = true
        closeBtn.layer?.backgroundColor = NSColor.windowBackgroundColor.withAlphaComponent(0.8).cgColor
        closeBtn.layer?.cornerRadius = 9
        closeBtn.isHidden = true
        closeBtn.target = self
        closeBtn.action = #selector(closeClicked)
        closeBtn.autoresizingMask = [.maxXMargin, .minYMargin]

        // Status icon (emoji-ish glyph) on the right.
        iconView.frame = NSRect(x: cardWidth - 34, y: cardHeight - 32, width: 24, height: 20)
        iconView.font = NSFont.systemFont(ofSize: 15)
        iconView.alignment = .center
        iconView.backgroundColor = .clear
        iconView.isBezeled = false; iconView.isEditable = false
        iconView.autoresizingMask = [.minXMargin]

        // Spinning indicator, shown for running/thinking (overlaps the icon slot),
        // so the "working" state reads as live motion rather than a static glyph.
        spinner.frame = NSRect(x: cardWidth - 32, y: cardHeight - 30, width: 16, height: 16)
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        spinner.autoresizingMask = [.minXMargin]

        // Row 1 — title (the user's latest question).
        titleLabel.frame = NSRect(x: 14, y: cardHeight - 26, width: cardWidth - 52, height: 18)
        titleLabel.font = NSFont.boldSystemFont(ofSize: 13)
        titleLabel.textColor = .labelColor
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.backgroundColor = .clear; titleLabel.isBezeled = false; titleLabel.isEditable = false
        titleLabel.autoresizingMask = [.width]

        // Row 2 — subtitle (the surface: Claude Code · Cursor).
        subtitleLabel.frame = NSRect(x: 14, y: cardHeight - 44, width: cardWidth - 52, height: 15)
        subtitleLabel.font = NSFont.systemFont(ofSize: 11)
        subtitleLabel.textColor = .tertiaryLabelColor
        subtitleLabel.lineBreakMode = .byTruncatingTail
        subtitleLabel.backgroundColor = .clear; subtitleLabel.isBezeled = false; subtitleLabel.isEditable = false
        subtitleLabel.autoresizingMask = [.width]

        // Row 3 — body (current activity / reply text).
        bodyLabel.frame = NSRect(x: 14, y: 8, width: cardWidth - 52, height: 18)
        bodyLabel.font = NSFont.systemFont(ofSize: 12)
        bodyLabel.textColor = .secondaryLabelColor
        bodyLabel.lineBreakMode = .byTruncatingTail
        bodyLabel.backgroundColor = .clear; bodyLabel.isBezeled = false; bodyLabel.isEditable = false
        bodyLabel.autoresizingMask = [.width]

        card.addSubview(titleLabel)
        card.addSubview(subtitleLabel)
        card.addSubview(bodyLabel)
        card.addSubview(iconView)
        card.addSubview(spinner)
        card.addSubview(closeBtn)
        panel.contentView = card
    }

    @objc private func closeClicked() { onClose?() }

    private func setHover(_ h: Bool) { closeBtn.isHidden = !h }

    func setCollapsible(_ on: Bool) { /* collapse control now lives on the pet */ }

    private func icon(for state: String) -> String {
        switch state {
        case "attention": return "❓"
        case "waiting": return "⏳"
        case "failed", "error": return "⚠️"
        case "review", "reply": return "✅"
        case "canceled": return "⛔"
        case "thinking": return "🧠"
        case "running", "tool": return "🏃🏻‍♂️‍➡️"
        default: return "·"
        }
    }

    func update(title: String, subtitle: String, body: String, state: String) {
        titleLabel.stringValue = title
        subtitleLabel.stringValue = subtitle
        bodyLabel.stringValue = body
        // Working states (thinking/running/tool) → live spinner; everything else
        // keeps its static status glyph.
        let spinning = (state == "thinking" || state == "running" || state == "tool")
        if spinning {
            iconView.stringValue = ""
            spinner.startAnimation(nil)
        } else {
            spinner.stopAnimation(nil)
            iconView.stringValue = icon(for: state)
        }
    }

    func setFrameTopLeft(_ topLeft: NSPoint) {
        panel.setFrameTopLeftPoint(topLeft)
    }
    func show() { panel.orderFrontRegardless() }
    func hide() { panel.orderOut(nil) }
    var height: CGFloat { cardHeight }
    var width: CGFloat { cardWidth }
}

// ── Collapsed badge: a small "▾ N" pill shown when the stack is collapsed ────────

final class BadgeWindow: NSObject {
    let panel: NSPanel
    var onClick: (() -> Void)?
    private let label = NSTextField(labelWithString: "")
    let width: CGFloat = 64
    let height: CGFloat = 28

    override init() {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: height),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        super.init()
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.hidesOnDeactivate = false

        let blur = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        blur.material = .hudWindow; blur.state = .active; blur.blendingMode = .behindWindow
        blur.wantsLayer = true; blur.layer?.cornerRadius = 14; blur.layer?.masksToBounds = true
        blur.autoresizingMask = [.width, .height]

        let v = CardView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        v.wantsLayer = true
        v.onClick = { [weak self] in self?.onClick?() }
        v.autoresizingMask = [.width, .height]
        v.addSubview(blur)

        label.frame = NSRect(x: 0, y: 4, width: width, height: 20)
        label.alignment = .center
        label.font = NSFont.boldSystemFont(ofSize: 13)
        label.textColor = .labelColor
        label.backgroundColor = .clear; label.isBezeled = false; label.isEditable = false
        label.autoresizingMask = [.width]
        v.addSubview(label)
        panel.contentView = v
    }

    func setCount(_ n: Int) { label.stringValue = "▾ \(n)" }
    func setFrameTopLeft(_ p: NSPoint) { panel.setFrameTopLeftPoint(p) }
    func show() { panel.orderFrontRegardless() }
    func hide() { panel.orderOut(nil) }
}

func runSelftest() {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let pet = PetWindow()
    pet.centerOnScreen()   // center so it's easy to watch during QA
    pet.show()
    pet.setState("idle")

    let states = ["idle", "running", "waiting", "failed", "review", "running", "idle"]
    var i = 0
    let timer = DispatchSource.makeTimerSource(queue: .main)
    timer.schedule(deadline: .now() + 3, repeating: 3)
    timer.setEventHandler {
        let s = states[i % states.count]
        FileHandle.standardError.write(">>> state = \(s)\n".data(using: .utf8)!)
        pet.setState(s)
        i += 1
    }
    timer.resume()
    // keep a strong ref
    objc_setAssociatedObject(app, "petTimer", timer, .OBJC_ASSOCIATION_RETAIN)
    objc_setAssociatedObject(app, "pet", pet, .OBJC_ASSOCIATION_RETAIN)
    app.run()
}

// ── Daemon: Unix socket server driving the pet ──────────────────────────────

final class Daemon: NSObject {
    private let pet: PetWindow
    private var listenFD: Int32 = -1
    private let queue = DispatchQueue(label: "ccpet.socket")
    // Per-session latest state, to compute the aggregate mascot state.
    private var sessionStates: [String: String] = [:]
    // Per-session most-recent open command + timestamp (for click-to-jump).
    private var sessionOpenCmd: [String: String] = [:]
    private var sessionLastTs: [String: Double] = [:]
    private var sessionText: [String: String] = [:]     // latest activity/reply text
    private var sessionTitle: [String: String] = [:]     // card title (user question)
    private var sessionSubtitle: [String: String] = [:]  // card subtitle (surface)
    private var cards: [String: CardWindow] = [:]        // one notification card per session
    // Sessions the user dismissed (✕), with the state-category at dismissal.
    // The card reappears once the session moves to a different category:
    // dismissed-while-running → reappears on completion; dismissed-while-done →
    // reappears on the next prompt (thinking).
    private var dismissedCategory: [String: String] = [:]
    private var stackExpanded = true
    private var idleTimer: DispatchSourceTimer?
    private let idleShutdownSec = 600.0   // self-exit after 10 min with no activity
    private var sweepTimer: DispatchSourceTimer?
    // A running/thinking session with no new event for this long is treated as
    // interrupted/canceled (Claude Code fires no hook on user interrupt), so the
    // card doesn't sit stuck on "Running" forever.
    private let staleRunningSec = 90.0

    init(pet: PetWindow) {
        self.pet = pet
        super.init()
        // Clicking the pet body opens the Claude Code desktop app (like Codex
        // opens its main window) — does not switch sessions.
        pet.onClick = { [weak self] in self?.openClaudeApp() }
        // The collapse control (top-right of the pet) toggles the card stack.
        pet.onToggleCollapse = { [weak self] in self?.toggleStack() }
        // Dragging the pet re-anchors the notification cards.
        pet.onMoved = { [weak self] in self?.layoutCards() }
    }

    private func openClaudeApp() {
        let p = Process()
        p.launchPath = "/bin/sh"
        // Bring the Claude desktop app to the front (no session switch).
        p.arguments = ["-c", "open -a Claude 2>/dev/null || open -a 'Claude'"]
        try? p.run()
    }

    // True if a daemon is already listening on `path` (connect succeeds).
    private func isDaemonAlive(_ path: String) -> Bool {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        if fd < 0 { return false }
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        _ = withUnsafeMutablePointer(to: &addr.sun_path.0) { ptr in
            path.withCString { cs in strcpy(ptr, cs) }
        }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        let r = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, len) }
        }
        return r == 0
    }

    func start() {
        let path = socketPath()
        try? FileManager.default.createDirectory(
            atPath: (path as NSString).deletingLastPathComponent,
            withIntermediateDirectories: true)

        // Atomic singleton guard via an exclusive flock. Unlike a socket
        // check-then-bind (which has a race window where two concurrently-spawned
        // daemons both pass the check, then fight over the socket and one
        // flickers/dies), a held flock is atomic: exactly one process can hold
        // LOCK_EX at a time. The lock fd is intentionally leaked for the process
        // lifetime — the OS releases it on exit. This fixed "pet appears then
        // vanishes" on cold start when multiple hooks spawn daemons at once.
        let lockPath = "\(RUNTIME_DIR)/daemon.lock"
        let lockFD = open(lockPath, O_CREAT | O_RDWR, 0o644)
        if lockFD >= 0 && flock(lockFD, LOCK_EX | LOCK_NB) != 0 {
            FileHandle.standardError.write("another ccpet daemon holds the lock; exiting\n".data(using:.utf8)!)
            exit(0)
        }

        // We hold the lock — safe to (re)claim the socket. Clear any stale file.
        unlink(path)
        listenFD = socket(AF_UNIX, SOCK_STREAM, 0)
        guard listenFD >= 0 else { FileHandle.standardError.write("socket() failed\n".data(using:.utf8)!); exit(1) }

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        _ = withUnsafeMutablePointer(to: &addr.sun_path.0) { ptr in
            path.withCString { cs in strcpy(ptr, cs) }
        }
        let len = socklen_t(MemoryLayout<sockaddr_un>.size)
        let bindRes = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(listenFD, $0, len) }
        }
        guard bindRes == 0 else { FileHandle.standardError.write("bind() failed\n".data(using:.utf8)!); exit(1) }
        guard listen(listenFD, 16) == 0 else { FileHandle.standardError.write("listen() failed\n".data(using:.utf8)!); exit(1) }

        restoreFromDisk()
        armIdleTimer()
        armSweepTimer()

        queue.async { [weak self] in self?.acceptLoop() }
    }

    private func acceptLoop() {
        while true {
            let clientFD = accept(listenFD, nil, nil)
            if clientFD < 0 { continue }
            // Handle each client on its own thread so a slow/lingering connection
            // never blocks accept() (which would refuse later connections).
            DispatchQueue.global().async { [weak self] in self?.handleClient(clientFD) }
        }
    }

    private func handleClient(_ fd: Int32) {
        var buf = [UInt8](repeating: 0, count: 65536)
        var acc = Data()
        while true {
            let n = read(fd, &buf, buf.count)
            if n <= 0 { break }
            acc.append(contentsOf: buf[0..<n])
            while let nl = acc.firstIndex(of: 0x0A) {
                let line = acc.subdata(in: acc.startIndex..<nl)
                acc.removeSubrange(acc.startIndex...nl)
                if let reply = handleLine(line) {
                    reply.withUnsafeBytes { _ = write(fd, $0.baseAddress, reply.count) }
                }
            }
        }
        close(fd)
    }

    // Returns optional reply bytes (e.g. pong).
    private func handleLine(_ data: Data) -> Data? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = obj["type"] as? String else { return nil }
        switch type {
        case "ping":
            return "{\"type\":\"pong\"}\n".data(using: .utf8)
        case "switch_pet":
            if let pet = obj["pet"] as? String {
                DispatchQueue.main.async { [weak self] in
                    self?.pet.switchPet(pet)
                    // Re-lay-out cards + collapse control: switching pets rebuilds
                    // the sprite/layer, and the cards anchor to the pet frame, so
                    // they must be repositioned (else they vanish until the user
                    // toggles the stack twice).
                    self?.layoutCards()
                }
            }
            return nil
        case "hide":
            DispatchQueue.main.async { [weak self] in
                self?.pet.hidePet()
                for (_, c) in self?.cards ?? [:] { c.hide() }
            }
            return nil
        case "show":
            DispatchQueue.main.async { [weak self] in
                self?.pet.showPet(); self?.layoutCards()
            }
            return nil
        case "quit":
            DispatchQueue.main.async { NSApp.terminate(nil) }
            return nil
        case "state":
            let session = obj["session"] as? String ?? "default"
            let state = obj["state"] as? String ?? "idle"
            let text = obj["text"] as? String
            let openCmd = obj["open_cmd"] as? String
            let title = obj["title"] as? String
            let subtitle = obj["subtitle"] as? String
            DispatchQueue.main.async { [weak self] in
                self?.applyState(session: session, state: state, text: text,
                                 openCmd: openCmd, title: title, subtitle: subtitle)
            }
            return nil
        case "session_end":
            let session = obj["session"] as? String ?? ""
            DispatchQueue.main.async { [weak self] in
                self?.endSession(session)
            }
            return nil
        default:
            return nil
        }
    }

    private func applyState(session: String, state: String, text: String?,
                            openCmd: String?, title: String?, subtitle: String?) {
        // If this session's card was dismissed, clear the dismissal once the
        // session moves to a different state-category (running↔done/etc), so the
        // card reappears exactly on the next meaningful transition.
        if let dismissed = dismissedCategory[session],
           stateCategory(state) != dismissed {
            dismissedCategory.removeValue(forKey: session)
        }
        sessionStates[session] = state
        if let c = openCmd, !c.isEmpty { sessionOpenCmd[session] = c }
        if let t = text, !t.isEmpty { sessionText[session] = t }
        if let ti = title, !ti.isEmpty { sessionTitle[session] = ti }
        if let st = subtitle, !st.isEmpty { sessionSubtitle[session] = st }
        sessionLastTs[session] = Date().timeIntervalSince1970
        recomputeAggregate(retrigger: true)
        updateCards()
        persistToDisk()
        armIdleTimer()
    }

    // Coarse state grouping for the dismiss/reappear rule. "running" covers the
    // in-flight phase; "done" covers every terminal/attention state.
    private func stateCategory(_ s: String) -> String {
        switch s {
        case "running", "thinking", "tool": return "running"
        default: return "done"
        }
    }

    // User dismissed a card via ✕. Remember the category so it can reappear.
    private func dismissCard(_ session: String) {
        dismissedCategory[session] = stateCategory(sessionStates[session] ?? "idle")
        cards[session]?.hide()
        cards.removeValue(forKey: session)
        recomputeAggregate()
        updateCards()
    }

    private func endSession(_ session: String) {
        sessionStates.removeValue(forKey: session)
        sessionOpenCmd.removeValue(forKey: session)
        sessionText.removeValue(forKey: session)
        sessionTitle.removeValue(forKey: session)
        sessionSubtitle.removeValue(forKey: session)
        sessionLastTs.removeValue(forKey: session)
        dismissedCategory.removeValue(forKey: session)
        cards[session]?.hide()
        cards.removeValue(forKey: session)
        recomputeAggregate()
        updateCards()
    }

    private func toggleStack() {
        stackExpanded.toggle()
        updateCards()
    }

    // ── Notification cards ──────────────────────────────────────────────────
    private func stateLabel(_ s: String) -> String {
        switch s {
        case "attention": return "Needs your input"
        case "waiting": return "Needs input"
        case "failed", "error": return "Blocked"
        case "review", "reply": return "Ready"
        case "canceled": return "Stopped"
        case "running", "thinking", "tool": return "Running"
        default: return "Idle"
        }
    }

    // Sessions worth showing a card for, newest first, capped.
    private func visibleSessions() -> [String] {
        let active = sessionLastTs.keys.filter {
            (sessionStates[$0] ?? "idle") != "idle" || sessionText[$0] != nil
        }.filter { dismissedCategory[$0] == nil }   // hide user-dismissed cards
        let sorted = active.sorted { (sessionLastTs[$0] ?? 0) > (sessionLastTs[$1] ?? 0) }
        return Array(sorted.prefix(7))
    }

    // Rebuild card content + membership, then position everything.
    private func updateCards() {
        let visible = visibleSessions()
        let visibleSet = Set(visible)
        for (sid, card) in cards where !visibleSet.contains(sid) {
            card.hide(); cards.removeValue(forKey: sid)
        }
        for sid in visible {
            let card = cards[sid] ?? {
                let c = CardWindow()
                c.onClick = { [weak self] in self?.openSession(sid) }
                c.onClose = { [weak self] in self?.dismissCard(sid) }
                cards[sid] = c
                return c
            }()
            let body = sessionText[sid] ?? stateLabel(sessionStates[sid] ?? "idle")
            card.update(title: sessionTitle[sid] ?? "Claude Code",
                        subtitle: sessionSubtitle[sid] ?? "",
                        body: body,
                        state: sessionStates[sid] ?? "idle")
        }
        layoutCards()
    }

    // Collapsed-badge tint: blue=running, green=review/done, red=failed.
    // Priority gray < blue < green < brown < red (later wins) across sessions.
    private func badgeColor(for sessions: [String]) -> NSColor? {
        var rank = 0  // 0 none, 1 gray, 2 blue, 3 green, 4 brown, 5 red
        for sid in sessions {
            switch sessionStates[sid] ?? "idle" {
            case "canceled":                    rank = max(rank, 1)
            case "running", "thinking", "tool": rank = max(rank, 2)
            case "review", "reply":             rank = max(rank, 3)
            case "attention", "waiting":        rank = max(rank, 4)
            case "failed", "error":             rank = max(rank, 5)
            default: break
            }
        }
        switch rank {
        case 1: return NSColor.systemGray.withAlphaComponent(0.9)
        case 2: return NSColor.systemBlue.withAlphaComponent(0.92)
        case 3: return NSColor.systemGreen.withAlphaComponent(0.92)
        case 4: return NSColor.systemBrown.withAlphaComponent(0.95)
        case 5: return NSColor.systemRed.withAlphaComponent(0.92)
        default: return nil
        }
    }

    // Position cards above the pet, and drive the pet's collapse control.
    private func layoutCards() {
        let visible = visibleSessions()
        // No cards → hide the collapse control and any leftover cards.
        guard !visible.isEmpty else {
            pet.setCollapseControl(visible: false, collapsed: false, count: 0)
            for (_, c) in cards { c.hide() }
            return
        }
        pet.setCollapseControl(visible: true, collapsed: !stackExpanded,
                               count: visible.count, badgeColor: badgeColor(for: visible))

        if !stackExpanded {
            for (_, c) in cards { c.hide() }
            return
        }

        // Expanded → stack cards upward, newest closest to the pet.
        let petFrame = pet.frame
        let leftX = petFrame.midX - 150   // card width 300, centered on pet
        var y = petFrame.maxY + 10
        for sid in visible {
            guard let card = cards[sid] else { continue }
            y += card.height + 8
            card.setFrameTopLeft(NSPoint(x: leftX, y: y))
            card.show()
        }
    }

    private func openSession(_ sid: String) {
        if let cmd = sessionOpenCmd[sid], !cmd.isEmpty {
            let p = Process()
            p.launchPath = "/bin/sh"
            p.arguments = ["-c", cmd]
            try? p.run()
        }
        // A completed/idle session's card is dismissed after the user jumps to it
        // (matches Codex: finished notifications clear on open). Active sessions
        // keep their card so ongoing work stays visible.
        let st = sessionStates[sid] ?? "idle"
        if st == "review" || st == "reply" || st == "idle" {
            endSession(sid)
        }
    }

    // Mascot shows the highest-priority state across all sessions.
    // Codex-faithful: each incoming activity event RE-TRIGGERS the animation
    // (the row plays 3× then settles into a slow idle loop). Without re-triggering
    // on same-state events, the pet would freeze after the first 3 cycles even
    // while work continues — so we replay whenever fresh activity arrives.
    private func recomputeAggregate(retrigger: Bool = false) {
        pruneStaleSessions()
        var best = "idle"
        var bestP = -1
        for (_, s) in sessionStates {
            let p = priority(s)
            if p > bestP { bestP = p; best = s }
        }
        if best != pet.animator.currentState || retrigger {
            pet.setState(best)
        }
    }

    // Drop sessions with no activity for a while (zombie sessions that never
    // sent session_end), so the mascot + notification stack stay current.
    private let sessionTTL = 900.0   // 15 min
    private func pruneStaleSessions() {
        let now = Date().timeIntervalSince1970
        for (sid, ts) in sessionLastTs where now - ts > sessionTTL {
            sessionStates.removeValue(forKey: sid)
            sessionOpenCmd.removeValue(forKey: sid)
            sessionText.removeValue(forKey: sid)
            sessionTitle.removeValue(forKey: sid)
            sessionSubtitle.removeValue(forKey: sid)
            sessionLastTs.removeValue(forKey: sid)
            cards[sid]?.hide()
            cards.removeValue(forKey: sid)
        }
    }

    // A running/thinking session that goes silent past staleRunningSec was most
    // likely interrupted by the user (no hook fires on interrupt). Transition it
    // to the terminal `canceled` state so the card shows "Stopped" (gray ⛔)
    // instead of sitting stuck on "Running". Returns true if anything changed.
    @discardableResult
    private func sweepStaleRunning() -> Bool {
        let now = Date().timeIntervalSince1970
        var changed = false
        for (sid, ts) in sessionLastTs {
            let st = sessionStates[sid] ?? "idle"
            let active = (st == "running" || st == "thinking" || st == "tool")
            if active && now - ts > staleRunningSec {
                sessionStates[sid] = "canceled"
                sessionText[sid] = "Stopped"
                // Bump the timestamp so the canceled card lives out its own TTL
                // window rather than being pruned immediately.
                sessionLastTs[sid] = now
                changed = true
            }
        }
        return changed
    }

    // Periodic sweep: interrupts leave no hook, so we poll to catch stuck
    // running cards and settle the mascot back to idle. Also auto-shuts-down
    // when the last Claude Code instance exits.
    private func armSweepTimer() {
        sweepTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 15, repeating: 15)
        t.setEventHandler { [weak self] in
            guard let self = self else { return }
            if self.sweepStaleRunning() {
                self.recomputeAggregate()
                self.updateCards()
                self.persistToDisk()
            }
            self.checkClaudeCodeAlive()
        }
        sweepTimer = t
        t.resume()
    }

    // Exit when the last Claude Code CLI instance is gone. We only start
    // watching once at least one instance has been seen — otherwise a pet woken
    // manually (`/pet on`) before any session exists would quit immediately.
    private var sawClaudeCode = false
    private func checkClaudeCodeAlive() {
        let n = liveClaudeCodeCount()
        if n > 0 {
            sawClaudeCode = true
            return
        }
        if sawClaudeCode {
            // Last Claude Code closed → tear down (matches "quit").
            exit(0)
        }
    }

    // Count real Claude Code CLI processes: the binary is literally named
    // `claude` (terminal, editor extensions, and the desktop-launched CLI all
    // exec a file whose basename is `claude`). This excludes the Electron
    // `Claude` / `Claude Helper` desktop app and our own `ccpet` daemon.
    private func liveClaudeCodeCount() -> Int {
        let p = Process()
        p.launchPath = "/bin/ps"
        p.arguments = ["-axo", "comm="]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do { try p.run() } catch { return 1 }   // on error, assume alive (don't quit)
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard let out = String(data: data, encoding: .utf8) else { return 1 }
        var count = 0
        for line in out.split(separator: "\n") {
            let comm = line.trimmingCharacters(in: .whitespaces)
            if comm.isEmpty { continue }
            let base = (comm as NSString).lastPathComponent
            if base == "claude" { count += 1 }
        }
        return count
    }

    // ── Durable state (survives daemon restart) ─────────────────────────────
    private var stateDir: String { "\(RUNTIME_DIR)/state" }
    private func persistToDisk() {
        try? FileManager.default.createDirectory(atPath: stateDir, withIntermediateDirectories: true)
        let obj: [String: Any] = ["sessions": sessionStates]
        if let d = try? JSONSerialization.data(withJSONObject: obj) {
            try? d.write(to: URL(fileURLWithPath: "\(stateDir)/aggregate.json"))
        }
    }
    private func restoreFromDisk() {
        let p = "\(stateDir)/aggregate.json"
        guard let d = FileManager.default.contents(atPath: p),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let s = obj["sessions"] as? [String: String] else { return }
        sessionStates = s
        // Restored sessions get a fresh timestamp so TTL still applies.
        let now = Date().timeIntervalSince1970
        for k in s.keys { sessionLastTs[k] = now }
        DispatchQueue.main.async { [weak self] in self?.recomputeAggregate() }
    }

    // ── Idle self-shutdown ──────────────────────────────────────────────────
    private func armIdleTimer() {
        idleTimer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + idleShutdownSec)
        t.setEventHandler {
            // Exit only if nothing is active.
            let active = self.sessionStates.values.contains { priority($0) > 0 }
            if !active { exit(0) }
        }
        idleTimer = t
        t.resume()
    }
}

// ── Entry point ─────────────────────────────────────────────────────────────

let args = CommandLine.arguments
if args.contains("--selftest") {
    runSelftest()
} else {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let pet = PetWindow()
    if ProcessInfo.processInfo.environment["CCPET_CENTER"] == "1" { pet.centerOnScreen() }
    pet.show()
    pet.setState("idle")
    let daemon = Daemon(pet: pet)
    daemon.start()
    objc_setAssociatedObject(app, "pet", pet, .OBJC_ASSOCIATION_RETAIN)
    objc_setAssociatedObject(app, "daemon", daemon, .OBJC_ASSOCIATION_RETAIN)
    app.run()
}
