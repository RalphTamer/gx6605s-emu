import SwiftUI
import AppKit

final class EmulatorModel: ObservableObject {
    @Published var firmwarePath = ""
    @Published var profile = "receiver-runtime"
    @Published var status = "Ready"
    @Published var running = false
    @Published var steps = "0"
    @Published var pc = "—"
    @Published var uart = ""
    @Published var registers = ""
    @Published var instructions = ""
    @Published var notes = ""
    @Published var input = ""
    @Published var memoryAddress = "0x100000"
    @Published var memory = ""
    @Published var breakpoint = ""
    @Published var displayImage: NSImage?
    @Published var displayStatus = "Waiting for guest display initialization"
    private var process: Process?
    private var stdinPipe: Pipe?
    private var buffer = Data()
    private var latest: [String: Any] = [:]
    private var selfTestStarted = false
    private var loadedFirmware = ""

    var firmwareName: String {
        firmwarePath.isEmpty ? "No firmware selected" : URL(fileURLWithPath: firmwarePath).lastPathComponent
    }

    init() {
        if let resource = Bundle.main.resourceURL,
           let root = try? String(contentsOf: resource.appendingPathComponent("workspace.txt"), encoding: .utf8) {
            let directory = URL(fileURLWithPath: root.trimmingCharacters(in: .whitespacesAndNewlines))
            let backup = directory.appendingPathComponent("traces/serial-flash-dump-20261113T184136405587Z.flash.bin").path
            firmwarePath = FileManager.default.fileExists(atPath: backup)
                ? backup
                : directory.appendingPathComponent("SR-4040HD_VEGA_V3.93_14102026.bin").path
        }
    }

    func chooseFirmware() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose the receiver firmware image"
        if panel.runModal() == .OK, let url = panel.url {
            firmwarePath = url.path
            start()
        }
    }

    func chooseProfile() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.message = "Choose a machine profile JSON file"
        if panel.runModal() == .OK, let url = panel.url {
            profile = url.path
            send("reset", ["profile": profile])
        }
    }

    func start() {
        stopBackend()
        guard FileManager.default.fileExists(atPath: firmwarePath), let resource = Bundle.main.resourceURL else {
            status = "Choose a firmware image first."
            return
        }
        let task = Process()
        let input = Pipe(), output = Pipe(), errors = Pipe()
        let pythonPath = (try? String(contentsOf: resource.appendingPathComponent("python-path.txt"), encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "/usr/bin/python3"
        task.executableURL = URL(fileURLWithPath: pythonPath)
        task.arguments = ["-u", "-m", "starsat_emu", "bridge", firmwarePath, "--profile", profile]
        task.currentDirectoryURL = resource.appendingPathComponent("python")
        task.standardInput = input
        task.standardOutput = output
        task.standardError = errors
        stdinPipe = input
        process = task
        loadedFirmware = firmwarePath
        buffer = Data()
        latest = [:]
        displayImage = nil
        displayStatus = "Waiting for guest display initialization"
        uart = ""
        instructions = ""
        status = "Loading firmware…"
        output.fileHandleForReading.readabilityHandler = { [weak self, weak task] handle in
            let data = handle.availableData
            guard !data.isEmpty, let self = self else { return }
            DispatchQueue.main.async {
                guard self.process === task else { return }
                self.buffer.append(data)
                while let newline = self.buffer.firstIndex(of: 10) {
                    let line = self.buffer.subdata(in: 0..<newline)
                    self.buffer.removeSubrange(0...newline)
                    if let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                        self.receive(object)
                    }
                }
            }
        }
        errors.fileHandleForReading.readabilityHandler = { [weak self, weak task] handle in
            let data = handle.availableData
            if !data.isEmpty, let message = String(data: data, encoding: .utf8) {
                DispatchQueue.main.async {
                    guard self?.process === task else { return }
                    self?.status = message.trimmingCharacters(in: .whitespacesAndNewlines)
                }
            }
        }
        task.terminationHandler = { [weak self] task in
            DispatchQueue.main.async {
                guard self?.process === task else { return }
                self?.running = false
                self?.status = "Backend exited (\(task.terminationStatus))."
            }
        }
        do { try task.run() }
        catch { status = "Could not start Python: \(error.localizedDescription)"; process = nil }
    }

    func stopBackend() {
        if let task = process, task.isRunning { task.terminate() }
        process = nil
        stdinPipe = nil
        running = false
    }

    func send(_ command: String, _ values: [String: Any] = [:]) {
        if process == nil || process?.isRunning != true || loadedFirmware != firmwarePath { start() }
        guard process?.isRunning == true, let pipe = stdinPipe else { return }
        var object = values
        object["command"] = command
        do {
            var data = try JSONSerialization.data(withJSONObject: object)
            data.append(10)
            try pipe.fileHandleForWriting.write(contentsOf: data)
        } catch { status = "Backend communication failed: \(error.localizedDescription)" }
    }

    func receive(_ event: [String: Any]) {
        switch event["event"] as? String {
        case "error": status = event["message"] as? String ?? "Backend error"
        case "memory":
            let hex = event["data"] as? String ?? ""
            let bytes = stride(from: 0, to: hex.count, by: 2).map { offset -> String in
                let start = hex.index(hex.startIndex, offsetBy: offset)
                return String(hex[start..<hex.index(start, offsetBy: 2)])
            }
            memory = stride(from: 0, to: bytes.count, by: 16).map { index in
                bytes[index..<min(index + 16, bytes.count)].joined(separator: " ")
            }.joined(separator: "\n")
        case "state":
            guard let state = event["state"] as? [String: Any], let cpu = state["cpu"] as? [String: Any] else { return }
            latest = state
            running = event["running"] as? Bool ?? false
            steps = (cpu["steps"] as? NSNumber)?.stringValue ?? "0"
            pc = cpu["pc"] as? String ?? "—"
            let reason = state["reason"] as? String ?? "paused"
            status = running ? "Running" : reason + ((state["detail"] as? String).map { $0.isEmpty ? "" : ": " + $0 } ?? "")
            uart = state["uart_output_text"] as? String ?? ""
            if let display = state["display"] as? [String: Any] {
                if display["status"] as? String == "ready" {
                    if let encoded = display["png_base64"] as? String, let data = Data(base64Encoded: encoded) {
                        displayImage = NSImage(data: data)
                    }
                    displayStatus = "OSD Ready • \(display["width"] ?? 0) × \(display["height"] ?? 0)"
                } else {
                    displayImage = nil
                    displayStatus = display["detail"] as? String ?? "Guest display unavailable"
                }
            }
            let values = cpu["registers"] as? [String] ?? []
            registers = values.enumerated().map { String(format: "r%-2d  %@", $0.offset, $0.element) }.joined(separator: "\n")
            registers += "\n\nCarry: \(cpu["carry"] ?? 0)"
            instructions = (state["recent_instructions"] as? [[String: Any]] ?? []).suffix(14).map {
                "\($0["pc"] ?? "")  \($0["instruction"] ?? "")"
            }.joined(separator: "\n")
            let profileState = state["profile"] as? [String: Any] ?? [:]
            notes = (profileState["assumptions"] as? [String] ?? []).joined(separator: "\n\n")
            if CommandLine.arguments.contains("--self-test") {
                if !selfTestStarted {
                    selfTestStarted = true
                    send("run", ["count": 100000000])
                } else if !running && reason != "paused" {
                    let summary: [String: Any] = ["native_app_test": "completed", "reason": reason,
                                                  "steps": cpu["steps"] ?? 0, "uart": uart]
                    if let data = try? JSONSerialization.data(withJSONObject: summary), let text = String(data: data, encoding: .utf8) {
                        print(text)
                        fflush(stdout)
                    }
                    stopBackend()
                    NSApplication.shared.terminate(nil)
                }
            }
        default: break
        }
    }

    func saveReport() {
        guard !latest.isEmpty else { return }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "starsat-emulator-report.json"
        if panel.runModal() == .OK, let url = panel.url {
            if url.standardizedFileURL.resolvingSymlinksInPath().path == URL(fileURLWithPath: firmwarePath).standardizedFileURL.resolvingSymlinksInPath().path {
                status = "Choose a report destination different from the firmware."
                return
            }
            do {
                let data = try JSONSerialization.data(withJSONObject: latest, options: [.prettyPrinted, .sortedKeys])
                try data.write(to: url, options: .atomic)
            } catch { status = "Could not save report: \(error.localizedDescription)" }
        }
    }
}

struct RemoteButton: View {
    let title: String
    var width: CGFloat = 56
    var height: CGFloat = 32
    var tint: Color = Color(white: 0.20)
    var foreground: Color = .white
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.system(size: 12, weight: .semibold, design: .rounded))
                .foregroundColor(foreground)
                .frame(width: width, height: height)
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(tint))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Color.white.opacity(0.09), lineWidth: 1))
        }
        .buttonStyle(.plain)
    }
}

struct RemotePad: View {
    let sendKey: (UInt32) -> Void
    @State private var customHex = "0xbfbf9768"
    @State private var lastSent = ""
    @State private var showCustom = false

    private func nec(_ cust: UInt16, _ cmd: UInt8) -> UInt32 {
        (UInt32(cust) << 16) | (UInt32(cmd) << 8) | UInt32(~cmd & 0xff)
    }

    private func trigger(_ label: String, _ code: UInt32) {
        lastSent = label
        sendKey(code)
    }

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                Text("REMOTE")
                    .font(.system(size: 11, weight: .bold, design: .rounded))
                    .foregroundColor(.secondary)
                    .tracking(1.1)
                Spacer()
                if !lastSent.isEmpty {
                    Text(lastSent)
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundColor(.cyan)
                }
            }

            HStack(spacing: 8) {
                RemoteButton(title: "POWER", width: 82, tint: Color.red.opacity(0.75)) { trigger("POWER", nec(0xbfbf, 0xaf)) }
                RemoteButton(title: "MUTE", width: 82) { trigger("MUTE", nec(0xbfbf, 0x67)) }
            }

            HStack(spacing: 8) {
                RemoteButton(title: "MENU", width: 82, tint: Color.blue.opacity(0.65)) { trigger("MENU", nec(0xbfbf, 0x97)) }
                RemoteButton(title: "EXIT", width: 82) { trigger("EXIT", nec(0xbfbf, 0x07)) }
            }

            // D-Pad
            VStack(spacing: 6) {
                RemoteButton(title: "▲", width: 52, height: 34) { trigger("UP", nec(0xbfbf, 0x2f)) }
                HStack(spacing: 6) {
                    RemoteButton(title: "◀", width: 52, height: 34) { trigger("LEFT", nec(0xbfbf, 0x77)) }
                    RemoteButton(title: "OK", width: 56, height: 34, tint: Color.cyan.opacity(0.85), foreground: .black) { trigger("OK", nec(0xbfbf, 0x4f)) }
                    RemoteButton(title: "▶", width: 52, height: 34) { trigger("RIGHT", nec(0xbfbf, 0xf7)) }
                }
                RemoteButton(title: "▼", width: 52, height: 34) { trigger("DOWN", nec(0xbfbf, 0x8f)) }
            }
            .padding(.vertical, 4)

            HStack(spacing: 8) {
                RemoteButton(title: "VOL+", width: 54) { trigger("VOL+", nec(0xbfbf, 0x73)) }
                RemoteButton(title: "VOL-", width: 54) { trigger("VOL-", nec(0xbfbf, 0xf3)) }
                RemoteButton(title: "INFO", width: 54) { trigger("INFO", nec(0xbfbf, 0x17)) }
            }

            DisclosureGroup("Custom NEC", isExpanded: $showCustom) {
                HStack(spacing: 6) {
                    TextField("0xNEC", text: $customHex)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(size: 11, design: .monospaced))
                    Button("Send") {
                        let cleaned = customHex.trimmingCharacters(in: .whitespacesAndNewlines)
                        let stripped = cleaned.hasPrefix("0x") || cleaned.hasPrefix("0X") ? String(cleaned.dropFirst(2)) : cleaned
                        if let code = UInt32(stripped, radix: 16) {
                            trigger("0x" + stripped, code)
                        }
                    }
                    .controlSize(.small)
                }
                .padding(.top, 4)
            }
            .font(.caption)
            .foregroundColor(.secondary)
        }
        .padding(14)
        .frame(width: 204)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color(white: 0.12)))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Color.white.opacity(0.08), lineWidth: 1))
    }
}

struct CompactRemoteStrip: View {
    let sendKey: (UInt32) -> Void

    private func nec(_ cust: UInt16, _ cmd: UInt8) -> UInt32 {
        (UInt32(cust) << 16) | (UInt32(cmd) << 8) | UInt32(~cmd & 0xff)
    }

    var body: some View {
        HStack(spacing: 8) {
            RemoteButton(title: "MENU", width: 62, tint: Color.blue.opacity(0.7)) { sendKey(nec(0xbfbf, 0x97)) }
            RemoteButton(title: "◀", width: 40) { sendKey(nec(0xbfbf, 0x77)) }
            RemoteButton(title: "▲", width: 40) { sendKey(nec(0xbfbf, 0x2f)) }
            RemoteButton(title: "OK", width: 48, tint: Color.cyan.opacity(0.85), foreground: .black) { sendKey(nec(0xbfbf, 0x4f)) }
            RemoteButton(title: "▼", width: 40) { sendKey(nec(0xbfbf, 0x8f)) }
            RemoteButton(title: "▶", width: 40) { sendKey(nec(0xbfbf, 0xf7)) }
            RemoteButton(title: "EXIT", width: 54) { sendKey(nec(0xbfbf, 0x07)) }
            RemoteButton(title: "VOL-", width: 52) { sendKey(nec(0xbfbf, 0xf3)) }
            RemoteButton(title: "VOL+", width: 52) { sendKey(nec(0xbfbf, 0x73)) }
            RemoteButton(title: "INFO", width: 50) { sendKey(nec(0xbfbf, 0x17)) }
            RemoteButton(title: "MUTE", width: 52) { sendKey(nec(0xbfbf, 0x67)) }
            RemoteButton(title: "POWER", width: 62, tint: Color.red.opacity(0.75)) { sendKey(nec(0xbfbf, 0xaf)) }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color(white: 0.12).opacity(0.92)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Color.white.opacity(0.1), lineWidth: 1))
    }
}

struct InspectorDrawer: View {
    @ObservedObject var model: EmulatorModel
    @State private var tab = 0
    @Binding var showAssumptions: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("", selection: $tab) {
                    Text("Serial UART").tag(0)
                    Text("Registers").tag(1)
                    Text("Disassembly").tag(2)
                    Text("Memory").tag(3)
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: 420)
                Spacer()
                Button("Assumptions") { showAssumptions = true }.controlSize(.small)
                Button("Save Report…") { model.saveReport() }.controlSize(.small)
            }

            Group {
                if tab == 0 {
                    VStack(spacing: 6) {
                        ScrollView {
                            Text(model.uart.isEmpty ? "Waiting for serial output…" : model.uart)
                                .font(.system(size: 11.5, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(8)
                        }
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.35)))
                        HStack {
                            TextField("Send command to UART…", text: $model.input)
                                .textFieldStyle(.roundedBorder)
                                .onSubmit { model.send("input", ["text": model.input + "\r"]); model.input = "" }
                            Button("Send") { model.send("input", ["text": model.input + "\r"]); model.input = "" }
                                .controlSize(.small)
                        }
                    }
                } else if tab == 1 {
                    ScrollView {
                        Text(model.registers)
                            .font(.system(size: 11.5, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                    }
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.35)))
                } else if tab == 2 {
                    ScrollView {
                        Text(model.instructions.isEmpty ? "No recent instructions." : model.instructions)
                            .font(.system(size: 11.5, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                    }
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.35)))
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            TextField("Address", text: $model.memoryAddress).textFieldStyle(.roundedBorder).frame(width: 120)
                            Button("Read 64B") { model.send("memory", ["address": model.memoryAddress, "size": 64]) }.controlSize(.small)
                            Divider().frame(height: 16)
                            TextField("Breakpoint", text: $model.breakpoint).textFieldStyle(.roundedBorder).frame(width: 120)
                            Button("Add BP") { model.send("breakpoint", ["address": model.breakpoint]) }.controlSize(.small)
                            Spacer()
                        }
                        ScrollView {
                            Text(model.memory.isEmpty ? "Read a RAM address to inspect bytes." : model.memory)
                                .font(.system(size: 11.5, design: .monospaced))
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(8)
                        }
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.black.opacity(0.35)))
                    }
                }
            }
            .frame(height: 165)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color(white: 0.11)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Color.white.opacity(0.08), lineWidth: 1))
    }
}

struct EmulatorView: View {
    @StateObject private var model = EmulatorModel()
    @State private var showAssumptions = false
    @State private var showInspector = false
    @State private var isFullScreen = false

    private func toggleFullScreenMode() {
        isFullScreen.toggle()
        if let window = NSApp.keyWindow ?? NSApp.mainWindow, window.styleMask.contains(.fullScreen) != isFullScreen {
            window.toggleFullScreen(nil)
        }
    }

    var body: some View {
        Group {
            if isFullScreen {
                VStack(spacing: 10) {
                    HStack {
                        HStack(spacing: 8) {
                            Circle().fill(model.running ? Color.green : Color.orange).frame(width: 8, height: 8)
                            Text(model.displayStatus).font(.headline).foregroundColor(.white)
                        }
                        Spacer()
                        Text("PC \(model.pc)  •  \(model.steps) steps")
                            .font(.caption.monospacedDigit())
                            .foregroundColor(.secondary)
                        Button("Fast Boot") { model.send("fast_boot") }.controlSize(.small)
                        Button(model.running ? "Pause" : "Run") { model.send(model.running ? "pause" : "run") }.controlSize(.small)
                        Button("Exit Full Screen") { toggleFullScreenMode() }
                            .keyboardShortcut("f", modifiers: [.command])
                            .controlSize(.small)
                    }
                    .padding(.horizontal, 18)
                    .padding(.top, 10)

                    ZStack {
                        Color.black
                        if let frame = model.displayImage {
                            Image(nsImage: frame).resizable().interpolation(.none).scaledToFit()
                        } else {
                            VStack(spacing: 8) {
                                Text("StarSat SR-4040HD Vega").font(.title3.weight(.semibold)).foregroundColor(.white.opacity(0.8))
                                Text(model.displayStatus).font(.callout).foregroundColor(.gray)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

                    CompactRemoteStrip { code in
                        model.send("remote", ["code": code])
                    }
                    .padding(.bottom, 12)
                }
                .background(Color.black)
                .preferredColorScheme(.dark)
            } else {
                VStack(spacing: 14) {
                    // Top Toolbar
                    HStack(spacing: 10) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("StarSat Emulator").font(.system(size: 16, weight: .bold))
                            Text(model.firmwareName)
                                .font(.caption)
                                .foregroundColor(.secondary)
                                .lineLimit(1)
                                .help(model.firmwarePath)
                        }
                        Button("Open…") { model.chooseFirmware() }.controlSize(.small)

                        Picker("", selection: Binding(get: { model.profile }, set: { value in
                            model.profile = value
                            model.send("reset", ["profile": value])
                        })) {
                            Text("OSD Runtime").tag("receiver-runtime")
                            Text("Observations").tag("receiver-observed")
                            Text("Exploratory").tag("exploratory")
                            Text("Strict").tag("strict")
                            if !["strict", "exploratory", "receiver-observed", "receiver-runtime"].contains(model.profile) {
                                Text("Custom").tag(model.profile)
                            }
                        }
                        .frame(width: 140)
                        .controlSize(.small)

                        Spacer()

                        HStack(spacing: 6) {
                            Button("Fast Boot") { model.send("fast_boot") }
                                .buttonStyle(.borderedProminent)
                                .tint(.cyan)
                                .controlSize(.small)
                            Button(model.running ? "Pause" : "Run") { model.send(model.running ? "pause" : "run") }
                                .controlSize(.small)
                            Button("Step") { model.send("step") }
                                .disabled(model.running)
                                .controlSize(.small)
                            Button("Reset") { model.send("reset", ["profile": model.profile]) }
                                .controlSize(.small)
                            Button(showInspector ? "Hide Inspector" : "Inspector") { showInspector.toggle() }
                                .keyboardShortcut("i", modifiers: [.command])
                                .controlSize(.small)
                            Button("Full Screen") { toggleFullScreenMode() }
                                .keyboardShortcut("f", modifiers: [.command])
                                .controlSize(.small)
                        }
                    }

                    // Main Viewport + Remote
                    HStack(alignment: .top, spacing: 16) {
                        VStack(spacing: 8) {
                            ZStack {
                                RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.black)
                                if let frame = model.displayImage {
                                    Image(nsImage: frame)
                                        .resizable()
                                        .interpolation(.none)
                                        .scaledToFit()
                                        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                                } else {
                                    VStack(spacing: 8) {
                                        Image(systemName: "tv")
                                            .font(.system(size: 36, weight: .light))
                                            .foregroundColor(.white.opacity(0.35))
                                        Text(model.displayStatus)
                                            .font(.callout)
                                            .foregroundColor(.gray)
                                        Text("Click Fast Boot to jump straight to the OSD")
                                            .font(.caption)
                                            .foregroundColor(.secondary)
                                    }
                                }
                            }
                            .aspectRatio(16.0 / 9.0, contentMode: .fit)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Color.white.opacity(0.08), lineWidth: 1))

                            HStack {
                                Circle().fill(model.running ? Color.green : Color.orange).frame(width: 7, height: 7)
                                Text(model.status).font(.caption).foregroundColor(.secondary).lineLimit(1)
                                Spacer()
                                Text("PC \(model.pc)  •  \(model.steps) instructions")
                                    .font(.caption.monospacedDigit())
                                    .foregroundColor(.secondary)
                            }
                            .padding(.horizontal, 4)
                        }

                        RemotePad { code in
                            model.send("remote", ["code": code])
                        }
                    }

                    if showInspector {
                        InspectorDrawer(model: model, showAssumptions: $showAssumptions)
                    }
                }
                .padding(16)
                .background(Color(white: 0.08))
                .preferredColorScheme(.dark)
            }
        }
        .frame(minWidth: 960, minHeight: showInspector ? 760 : 580)
        .sheet(isPresented: $showAssumptions) {
            VStack(alignment: .leading, spacing: 16) {
                Text("Machine Profile Assumptions").font(.headline)
                ScrollView {
                    Text(model.notes.isEmpty ? "Strict mode supplies no unverified device reset values." : model.notes)
                        .textSelection(.enabled)
                }
                Button("Close") { showAssumptions = false }
            }
            .padding(24)
            .frame(width: 650, height: 480)
        }
        .onAppear { model.start() }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didEnterFullScreenNotification)) { _ in isFullScreen = true }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didExitFullScreenNotification)) { _ in isFullScreen = false }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in model.stopBackend() }
    }
}

@main struct StarSatEmulatorApp: App {
    var body: some Scene {
        WindowGroup { EmulatorView() }
    }
}
