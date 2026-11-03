// `body.scroll-locked` while at least one modal layer is open (global.css): a count, so that a dialog over
// the drawer does not unlock the page when it closes.
let open = 0;

export function lockScroll(): () => void {
  open += 1;
  document.body.classList.add("scroll-locked");
  return () => {
    open -= 1;
    if (open === 0) {
      document.body.classList.remove("scroll-locked");
    }
  };
}
