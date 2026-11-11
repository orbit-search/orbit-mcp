/** Public SDK extension points for stable tool publication and validation error messages. */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import { publicToolSchema } from "./schema-compat.js";

import { scopeValidationErrors, normalizeValidationText } from "./validation-errors.js";

export class ContractMcpServer extends McpServer {
  override registerTool: McpServer["registerTool"] = (name, config, callback) => {
    const inputSchema = publicToolSchema(config.inputSchema);
    const outputSchema = publicToolSchema(config.outputSchema);
    scopeValidationErrors(inputSchema, outputSchema);
    return super.registerTool(name, { ...config, inputSchema, outputSchema }, callback);
  };

  override async connect(transport: Transport): Promise<void> {
    // Wrap the documented Transport API; never replace private SDK validation handlers.
    const wrapper: Transport = {
      start: async () => {
        transport.onmessage = (message, extra) => wrapper.onmessage?.(message, extra);
        transport.onerror = error => wrapper.onerror?.(error);
        const priorClose = transport.onclose;
        transport.onclose = () => { priorClose?.(); wrapper.onclose?.(); };
        await transport.start();
      },
      send: (message, options) => {
        if ("result" in message && message.result.isError === true && Array.isArray(message.result.content)) {
          message = { ...message, result: { ...message.result, content: message.result.content.map((item: unknown) => {
            if (item && typeof item === "object" && "type" in item && item.type === "text" && "text" in item && typeof item.text === "string") return { ...item, text: normalizeValidationText(item.text) };
            return item;
          }) } };
        }
        return transport.send(message, options);
      },
      close: () => transport.close(),
      get sessionId() { return transport.sessionId; },
      setProtocolVersion: version => transport.setProtocolVersion?.(version),
    };
    await super.connect(wrapper);
  }
}
