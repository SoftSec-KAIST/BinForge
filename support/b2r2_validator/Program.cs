using System.Text.Json;
using B2R2;
using B2R2.FrontEnd.BinFile;

const string Version = "0.1.0 (B2R2 0.10.1)";

if (args is ["--version"])
{
    Console.WriteLine(Version);
    return 0;
}

if (args.Length != 1 || !ExpectedISAs.TryGetValue(args[0], out var expected))
{
    Console.Error.WriteLine(
        "Usage: BinForge.B2R2Validator <x86_32|x86_64|arm_32|arm_64|" +
        "mips_32|mips_64|mipseb_32|mipseb_64|ppceb_32|ppceb_64|ppc_64>\n" +
        "Read one binary path per line from standard input.");
    return 2;
}

var fallback = new ISA(Architecture.Intel, WordSize.Bit64);
long checkedCount = 0;
long invalidCount = 0;
string? path;

while ((path = Console.ReadLine()) is not null)
{
    if (path.Length == 0)
        continue;

    checkedCount++;
    try
    {
        var bytes = File.ReadAllBytes(path);
        var detected = FormatDetector.Identify(bytes, fallback);
        var actual = detected.Item2;
        var valid = detected.Item1 == FileFormat.ELFBinary
                    && actual.Arch == expected.Arch
                    && actual.WordSize == expected.WordSize
                    && actual.Endian == expected.Endian;
        if (!valid)
        {
            invalidCount++;
            WriteFailure(path, args[0], detected.Item1.ToString(), actual, null);
        }
    }
    catch (Exception exception)
    {
        invalidCount++;
        WriteFailure(path, args[0], null, null, exception.Message);
    }
}

Console.WriteLine(JsonSerializer.Serialize(new
{
    expectedArch = args[0],
    checkedFiles = checkedCount,
    invalidFiles = invalidCount,
    b2r2Version = "0.10.1",
}));
return invalidCount == 0 ? 0 : 1;

static void WriteFailure(string path, string expectedArch, string? format,
                         ISA? actual, string? error)
{
    Console.Error.WriteLine(JsonSerializer.Serialize(new
    {
        path,
        expectedArch,
        format,
        architecture = actual?.Arch.ToString(),
        wordSize = actual?.WordSize.ToString(),
        endian = actual?.Endian.ToString(),
        error,
    }));
}

static class ExpectedISAs
{
    public static readonly IReadOnlyDictionary<string, ISA> All =
        new Dictionary<string, ISA>
        {
            ["x86_32"] = new(Architecture.Intel, Endian.Little, WordSize.Bit32),
            ["x86_64"] = new(Architecture.Intel, Endian.Little, WordSize.Bit64),
            ["arm_32"] = new(Architecture.ARMv7, Endian.Little, WordSize.Bit32),
            ["arm_64"] = new(Architecture.ARMv8, Endian.Little, WordSize.Bit64),
            ["mips_32"] = new(Architecture.MIPS, Endian.Little, WordSize.Bit32),
            ["mips_64"] = new(Architecture.MIPS, Endian.Little, WordSize.Bit64),
            ["mipseb_32"] = new(Architecture.MIPS, Endian.Big, WordSize.Bit32),
            ["mipseb_64"] = new(Architecture.MIPS, Endian.Big, WordSize.Bit64),
            ["ppceb_32"] = new(Architecture.PPC, Endian.Big, WordSize.Bit32),
            ["ppceb_64"] = new(Architecture.PPC, Endian.Big, WordSize.Bit64),
            ["ppc_64"] = new(Architecture.PPC, Endian.Little, WordSize.Bit64),
        };

    public static bool TryGetValue(string name, out ISA isa) =>
        All.TryGetValue(name, out isa!);
}
