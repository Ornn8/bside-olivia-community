using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Program
{
    internal static bool ShouldReport(int code) { return code != 0 && code != -1; }

    // The interface language the app saved (data\config\ui-language.json); Chinese unless English was chosen.
    internal static bool EnglishInterface(string root)
    {
        try
        {
            string saved = Path.Combine(root, "data", "config", "ui-language.json");
            return File.Exists(saved) && File.ReadAllText(saved).Contains("\"language\": \"en\"");
        }
        catch (Exception) { return false; }
    }

    internal static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    internal static int Launch(string root, string port)
    {
        string python = Path.GetFullPath(Path.Combine(root, @"..\runtime\python-3.12.10-embed-amd64\pythonw.exe"));
        string launcher = Path.Combine(root, @"launcher\version_launcher.py");
        if (!File.Exists(python)) return 2;
        if (!File.Exists(launcher)) return 2;
        var start = new ProcessStartInfo(python,
            Quote(launcher) + " --install-root " + Quote(root) + " start --port " + Quote(port));
        start.WorkingDirectory = root;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.WindowStyle = ProcessWindowStyle.Hidden;
        start.EnvironmentVariables["OLIVIA_PORT"] = port;
        using (var child = Process.Start(start))
        {
            child.WaitForExit();
            return child.ExitCode;
        }
    }

    [STAThread]
    private static int Main()
    {
        int code;
        string root = null;
        try
        {
            root = Path.GetDirectoryName(Application.ExecutablePath);
            string port = Environment.GetEnvironmentVariable("OLIVIA_PORT");
            if (String.IsNullOrEmpty(port)) port = "8899";
            code = Launch(root, port);
        }
        catch (System.ComponentModel.Win32Exception error) { code = error.NativeErrorCode; }
        catch (Exception) { code = 2; }
        if (ShouldReport(code))
        {
            string text = "Olivia 启动失败（错误码 " + code + "）。\r\n请将安装目录 data\\logs 下的启动日志提供给维护者。";
            if (root != null && EnglishInterface(root))
                text = "Olivia failed to start (error code " + code + ").\r\nPlease send the startup logs in the data\\logs folder of the installation to the maintainer.";
            MessageBox.Show(text, "Olivia", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        return code;
    }
}
