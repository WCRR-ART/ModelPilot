/** One request slot: replacements and unmounts invalidate all older responses. */
export class LatestRequest {
  private controller: AbortController | null = null;

  begin(): { signal: AbortSignal; isCurrent: () => boolean } {
    this.cancel();
    const controller = new AbortController();
    this.controller = controller;
    return {
      signal: controller.signal,
      isCurrent: () => this.controller === controller && !controller.signal.aborted,
    };
  }

  cancel(): void {
    this.controller?.abort();
    this.controller = null;
  }
}
