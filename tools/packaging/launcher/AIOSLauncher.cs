// Windows launcher for the portable AIOS build.
//
// Design notes - each of these is the result of something that did not work:
//
//   * pythonw.exe was unreliable here: failures during interpreter start-up
//     produced no window, no exit code the user ever saw, and no log. We start
//     the *console* python.exe inside a hidden cmd.exe instead, so stdout and
//     stderr can be redirected to a file that survives the crash.
//
//   * The redirect is done by cmd.exe rather than by .NET's RedirectStandardOutput
//     because the launcher exits as soon as the browser is open; a redirected
//     pipe with no reader would eventually block the server.
//
//   * The server is deliberately NOT a child we wait on. The launcher's whole
//     job is: start it, wait until it really answers, open the browser, leave.
//
// Compiled by tools/packaging/build_release.ps1 with the .NET Framework
// csc.exe (/target:winexe), because Windows PowerShell 5.1's Add-Type has no
// -CompilerOptions and therefore cannot set the icon or the subsystem.

using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Windows.Forms;

internal static class AIOSLauncher
{
    private const string Host = "127.0.0.1";
    private const int Port = 8765;

    // Generous: a first run on a cold filesystem has to import the whole
    // dependency tree and create the database before it will answer.
    private const int ReadyTimeoutSeconds = 120;

    private static string _root;
    private static string _logPath;

    [STAThread]
    private static int Main()
    {
        try
        {
            _root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            string python = Path.Combine(_root, "runtime", "python.exe");
            string app = Path.Combine(_root, "app.py");
            string logDir = Path.Combine(_root, "data", "logs");
            _logPath = Path.Combine(logDir, "launcher_console.log");
            string url = "http://" + Host + ":" + Port + "/";

            if (!File.Exists(python))
            {
                return Fail("Portable runtime is missing:\n\n" + python +
                            "\n\nThe package looks incomplete. Please unzip it again.");
            }
            if (!File.Exists(app))
            {
                return Fail("app.py is missing:\n\n" + app +
                            "\n\nThe package looks incomplete. Please unzip it again.");
            }

            Directory.CreateDirectory(logDir);

            // If a copy is already listening, just show it rather than starting
            // a second server on the same database.
            if (!IsServerReady())
            {
                StartServer(python, app);
            }

            if (!WaitForServer(ReadyTimeoutSeconds))
            {
                return Fail(
                    "AIOS did not finish starting within " + ReadyTimeoutSeconds + " seconds.\n\n" +
                    "The startup log may explain why:\n" + _logPath);
            }

            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            return 0;
        }
        catch (Exception ex)
        {
            return Fail("AIOS could not be started.\n\n" + ex.Message +
                        (_logPath == null ? "" : "\n\nStartup log:\n" + _logPath));
        }
    }

    private static void StartServer(string python, string app)
    {
        // cmd.exe /s /c "..." : with /s the outermost pair of quotes is stripped
        // and everything between is taken literally, which is the only reliable
        // way to quote three space-bearing paths on one command line.
        string command = "\"\"" + python + "\" \"" + app + "\" --no-browser > \"" +
                         _logPath + "\" 2>&1\"";

        ProcessStartInfo psi = new ProcessStartInfo("cmd.exe", "/d /s /c " + command);
        psi.WorkingDirectory = _root;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.WindowStyle = ProcessWindowStyle.Hidden;
        Process.Start(psi);
    }

    private static bool WaitForServer(int timeoutSeconds)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            if (IsServerReady()) return true;
            Thread.Sleep(250);
        }
        return false;
    }

    /// <summary>The port is listening AND the application answers on it.</summary>
    private static bool IsServerReady()
    {
        if (!PortIsOpen()) return false;
        try
        {
            HttpWebRequest request =
                (HttpWebRequest)WebRequest.Create("http://" + Host + ":" + Port + "/healthz");
            request.Timeout = 3000;
            request.ReadWriteTimeout = 3000;
            request.Method = "GET";
            using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
            {
                return response.StatusCode == HttpStatusCode.OK;
            }
        }
        catch
        {
            return false;
        }
    }

    private static bool PortIsOpen()
    {
        try
        {
            using (TcpClient client = new TcpClient())
            {
                IAsyncResult async = client.BeginConnect(Host, Port, null, null);
                if (!async.AsyncWaitHandle.WaitOne(500)) return false;
                client.EndConnect(async);
                return true;
            }
        }
        catch
        {
            return false;
        }
    }

    private static int Fail(string message)
    {
        MessageBox.Show(message, "AIOS Intelligence Monitor",
                        MessageBoxButtons.OK, MessageBoxIcon.Error);
        return 1;
    }
}
