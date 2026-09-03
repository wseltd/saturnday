// Vulnerable: dangerous XSS sinks
import React from "react";

export function UserGreeting({ name }: { name: string }) {
  return <div dangerouslySetInnerHTML={{ __html: `<h1>Hello ${name}</h1>` }} />;
}

export function setContent(el: HTMLElement, content: string) {
  el.innerHTML = content;
}
