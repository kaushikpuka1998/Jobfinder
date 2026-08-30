import Dashboard from "./Dashboard.jsx";
import Profile from "./Profile.jsx";

// Two pages, no router dependency — a plain <a> reloads into the other
// page's same index.html, and Flask always returns it for any path.
export default function App() {
  return window.location.pathname === "/profile" ? <Profile /> : <Dashboard />;
}
