import { Health } from "./pages/Health";
import { Item } from "./pages/Item";
import { User } from "./pages/User";

export const routes = [
  { path: "/health", element: <Health /> },
  { path: "/items/:version", element: <Item /> }, // :version is dotted, e.g. /items/v1.2
  { path: "/users/:id", element: <User /> },
];
