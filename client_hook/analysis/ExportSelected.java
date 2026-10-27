// Fixed-artifact, bounded function decompilation. No target execution.
// @category WeChat.Native
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.decompiler.*;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.SourceType;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;

public class ExportSelected extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        Path output = Paths.get(args[0]);
        Files.createDirectories(output);
        // Fixed-build stack probing helper preserves RAX. Without a callfixup
        // large stack-frame methods decompile with a spurious dynamic stack.
        Address probe = toAddr("186569e50");
        Function probeFunction = getFunctionAt(probe);
        if (probeFunction == null) {
            disassemble(probe);
            probeFunction = createFunction(probe, "__chkstk");
        }
        if (probeFunction != null) probeFunction.setCallFixup("alloca_probe");
        DecompInterface decompiler = new DecompInterface();
        decompiler.toggleCCode(true);
        decompiler.openProgram(currentProgram);
        for (String line : Files.readAllLines(Paths.get(args[1]))) {
            if (line.isBlank()) continue;
            String[] fields = line.split("\\s+");
            Address start = toAddr(fields[0]);
            Address end = toAddr(fields[1]);
            AddressSet body = new AddressSet(start, end.subtract(1));
            new DisassembleCommand(start, body, true).applyTo(currentProgram, monitor);
            Function function = getFunctionAt(start);
            if (function == null) {
                function = currentProgram.getFunctionManager().createFunction(
                    "native_" + start.toString(), start, body, SourceType.ANALYSIS);
            }
            DecompileResults result = decompiler.decompileFunction(function, 90, monitor);
            String name = start.toString();
            String text = result.decompileCompleted() ? result.getDecompiledFunction().getC() : result.getErrorMessage();
            Files.writeString(output.resolve(name + ".c"), text, StandardCharsets.UTF_8);
            StringBuilder assembly = new StringBuilder();
            InstructionIterator instructions = currentProgram.getListing().getInstructions(body, true);
            while (instructions.hasNext()) {
                Instruction instruction = instructions.next();
                assembly.append(instruction.getAddress()).append(" ").append(instruction).append("\n");
            }
            Files.writeString(output.resolve(name + ".asm"), assembly.toString(), StandardCharsets.UTF_8);
            println("EXPORTED " + name + " " + result.decompileCompleted());
        }
        decompiler.dispose();
    }
}
