import { Health } from "./pages/Health";
import { Item } from "./pages/Item";
import { User } from "./pages/User";

export const routes = [
  { path: "/health", element: <Health /> },
  { path: "/items/:id", element: <Item /> }, // :id is a UUID, e.g. /items/3f2b8c1e-9d4a-4e6b-8f0a-1c2d3e4f5a6b
  { path: "/users/:id", element: <User /> }, // :id is a UUID
];
