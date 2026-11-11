using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Chromo")]
[assembly: AssemblyProduct("chromo-bro")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class Launcher {
    private static string Quote(string value) {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { result.Append('\\', slashes * 2 + 1).Append(c); slashes = 0; continue; }
            result.Append('\\', slashes).Append(c); slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args) {
        string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        try {
            Application.EnableVisualStyles();
            if (args.Length > 0 && args[0] == "--notify") {
                using (NotifyIcon icon = new NotifyIcon()) {
                    icon.Icon = Icon.ExtractAssociatedIcon(Path.Combine(root, "Chromo.exe")) ?? SystemIcons.Application;
                    icon.Text = "Chromo";
                    icon.Visible = true;
                    icon.ShowBalloonTip(8000, "Chromo update", args.Length > 1 ? args[1] : "Run chromo status for details.", ToolTipIcon.Warning);
                    using (Timer timer = new Timer()) {
                        timer.Interval = 10000;
                        timer.Tick += delegate { timer.Stop(); Application.ExitThread(); };
                        timer.Start(); Application.Run();
                    }
                    icon.Visible = false;
                }
                return 0;
            }
            string python = Path.Combine(root, "runtime", "python", "pythonw.exe");
            string client = Path.Combine(root, "runtime", "chromo_client.py");
            if (!File.Exists(python) || !File.Exists(client)) throw new FileNotFoundException("Extract the complete Chromo package before starting it.");
            StringBuilder arguments = new StringBuilder(Quote(client) + " --root " + Quote(root) + " --gui");
#if SETUP
            if (args.Length == 0) arguments.Append(" setup");
#endif
            foreach (string arg in args) arguments.Append(" ").Append(Quote(arg));
            ProcessStartInfo start = new ProcessStartInfo(python, arguments.ToString());
            start.UseShellExecute = false; start.CreateNoWindow = true; start.WorkingDirectory = root;
            Process.Start(start);
            return 0;
        } catch (Exception error) {
            MessageBox.Show(error.Message, "Chromo", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
