// HTML ファイルを macOS 標準の WebKit で読み込み、A4 の PDF に書き出す(追加のパッケージは使わない)。
// 使い方: html2pdf <入力.html> <出力.pdf> <待ち時間の上限(秒)>
// 印刷用の見た目(ライト配色・改ページ)は HTML 側の @media print / @page で決める。
import AppKit
import WebKit

let a4 = NSSize(width: 595.28, height: 841.89)  // A4 [pt]
let marginPt: CGFloat = 0  // 余白は CSS の @page で指定する

final class Renderer: NSObject, WKNavigationDelegate {
    let webView: WKWebView
    let output: URL

    init(output: URL) {
        self.output = output
        webView = WKWebView(frame: NSRect(origin: .zero, size: a4), configuration: WKWebViewConfiguration())
        webView.appearance = NSAppearance(named: .aqua)  // ダークモードの環境でもライトで描く
        super.init()
        webView.navigationDelegate = self
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let info = NSPrintInfo()
        info.paperSize = a4
        info.topMargin = marginPt
        info.bottomMargin = marginPt
        info.leftMargin = marginPt
        info.rightMargin = marginPt
        info.horizontalPagination = .fit
        info.verticalPagination = .automatic
        info.jobDisposition = .save
        info.dictionary()[NSPrintInfo.AttributeKey.jobSavingURL] = output
        let op = webView.printOperation(with: info)
        op.showsPrintPanel = false
        op.showsProgressPanel = false
        op.view?.frame = webView.bounds  // 枠がないと白紙になるため
        op.runModal(for: NSWindow(), delegate: self, didRun: #selector(printDone(_:success:contextInfo:)), contextInfo: nil)
    }

    @objc func printDone(_ op: NSPrintOperation, success: Bool, contextInfo: UnsafeMutableRawPointer?) {
        exit(success ? 0 : 1)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        FileHandle.standardError.write("読み込みに失敗しました: \(error)\n".data(using: .utf8)!)
        exit(2)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        FileHandle.standardError.write("読み込みに失敗しました: \(error)\n".data(using: .utf8)!)
        exit(2)
    }
}

let args = CommandLine.arguments
guard args.count == 4, let timeout = Double(args[3]) else {
    FileHandle.standardError.write("使い方: html2pdf <入力.html> <出力.pdf> <待ち時間の上限(秒)>\n".data(using: .utf8)!)
    exit(64)
}
let input = URL(fileURLWithPath: args[1])
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
let renderer = Renderer(output: URL(fileURLWithPath: args[2]))
renderer.webView.loadFileURL(input, allowingReadAccessTo: input.deletingLastPathComponent())
DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
    FileHandle.standardError.write("時間内に終わりませんでした(\(timeout) 秒)\n".data(using: .utf8)!)
    exit(3)
}
app.run()
