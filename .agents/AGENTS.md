# UI Manipulation Rules

## Modal Visibility
When interacting with modals (e.g., elements with the `.modal-overlay` class) in JavaScript:
- **DO NOT** use `style.display = 'flex'` or `style.display = 'none'` to show or hide the modal.
- **DO** use `classList.add('is-active')` to show the modal and `classList.remove('is-active')` to hide it.
- **Reason**: Modals rely on CSS transitions (`opacity` and `visibility`) bound to the `.is-active` class for animations. Manipulating the `display` property breaks these animations and can leave the modal invisible.
