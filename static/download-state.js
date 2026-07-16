(function (global) {
  const STORAGE_KEY = "rotta-downloader-state";

  function clear(storage) {
    storage.removeItem(STORAGE_KEY);
  }

  function load(storage) {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }

    try {
      const saved = JSON.parse(raw);
      if (
        !saved ||
        typeof saved.sessionId !== "string" ||
        typeof saved.root !== "string" ||
        typeof saved.host !== "string" ||
        !["smb", "sftp"].includes(saved.protocol)
      ) {
        clear(storage);
        return null;
      }
      return {
        sessionId: saved.sessionId,
        root: saved.root,
        host: saved.host,
        protocol: saved.protocol,
      };
    } catch (error) {
      clear(storage);
      return null;
    }
  }

  function save(storage, state) {
    storage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        sessionId: state.sessionId,
        root: state.root,
        host: state.host,
        protocol: state.protocol,
      })
    );
  }

  const api = { STORAGE_KEY, clear, load, save };
  global.RottaState = api;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(globalThis);
