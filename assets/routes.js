(() => {
  "use strict";

  const data = window.ROUTE_DATA;
  if (!data || data.schemaVersion !== 1) {
    const message = document.getElementById("empty-state");
    message.textContent = "No route data has been published.";
    message.hidden = false;
    return;
  }
  const routes = document.getElementById("routes");
  const emptyState = document.getElementById("empty-state");
  const inspectorFilter = document.getElementById("inspector-filter");
  const routeSearch = document.getElementById("route-search");

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const formatDate = (value) => {
    if (!value) return "";
    return new Intl.DateTimeFormat("en-US", {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
      timeZone: "UTC"
    }).format(new Date(`${value}T00:00:00Z`));
  };

  const formatDateTime = (value) => {
    if (!value) return "";
    return new Intl.DateTimeFormat("en-US", {
      dateStyle: "medium",
      timeStyle: "short"
    }).format(new Date(value));
  };

  const labeledCell = (label, className = "") => {
    const cell = element("td", className);
    cell.dataset.label = label;
    return cell;
  };

  const addLines = (cell, values) => {
    const list = element("div", "item-list");
    values.filter(Boolean).forEach((value) => list.append(element("span", "", value)));
    cell.append(list);
  };

  const inspectionCell = (stop) => {
    const cell = labeledCell("Inspection #(s)");
    const list = element("div", "item-list");
    stop.inspections.forEach((inspection) => {
      const item = element("div");
      const link = element("a", "inspection-link", inspection.number || "Inspection");
      if (inspection.url) {
        link.href = inspection.url;
        link.target = "_blank";
        link.rel = "noopener";
      }
      item.append(link);
      if (inspection.status) item.append(element("div", "status", inspection.status));
      list.append(item);
    });
    cell.append(list);
    return cell;
  };

  const permitCell = (stop) => {
    const cell = labeledCell("Permit #(s)");
    const list = element("div", "item-list");
    const seen = new Set();
    stop.inspections.forEach((inspection) => {
      if (!inspection.permitNumber) return;
      const key = inspection.permitId || inspection.permitNumber;
      if (seen.has(key)) return;
      seen.add(key);
      if (inspection.permitUrl) {
        const link = element("a", "inspection-link", inspection.permitNumber);
        link.href = inspection.permitUrl;
        link.target = "_blank";
        link.rel = "noopener";
        list.append(link);
      } else {
        list.append(element("span", "", inspection.permitNumber));
      }
    });
    cell.append(list);
    return cell;
  };

  const uniqueValues = (stop, field) => [
    ...new Set(stop.inspections.map((item) => item[field]).filter(Boolean))
  ];

  const stopMatches = (stop, query) => {
    if (!query) return true;
    const searchable = [
      stop.address.line1,
      stop.address.line2,
      ...stop.inspections.flatMap((inspection) => [
        inspection.number,
        inspection.type,
        inspection.status,
        inspection.permitNumber
      ])
    ].join(" ").toLocaleLowerCase();
    return searchable.includes(query);
  };

  const renderStop = (stop) => {
    const row = document.createElement("tr");
    if (stop.isRollover) row.classList.add("rollover");

    const sequence = labeledCell("Sequence", "sequence");
    if (stop.isRollover) {
      sequence.append(element("span", "rollover-star", "*"));
      sequence.title = "Rollover priority";
    }
    sequence.append(document.createTextNode(String(stop.sequence)));
    row.append(sequence);

    const address = labeledCell("Address", "address");
    if (stop.needsAddressReview) {
      address.append(element("span", "address-review", "Address required"));
    } else {
      addLines(address, [stop.address.line1, stop.address.line2]);
    }
    row.append(address);
    row.append(inspectionCell(stop));

    row.append(permitCell(stop));

    const types = labeledCell("Inspection type(s)");
    addLines(types, uniqueValues(stop, "type"));
    row.append(types);
    return row;
  };

  const renderInspector = (inspector, stops) => {
    const section = element("section", "inspector-route");
    const heading = element("div", "inspector-heading");
    heading.append(element("h3", "", inspector.name));
    const inspectionCount = stops.reduce((sum, stop) => sum + stop.inspectionCount, 0);
    heading.append(element("p", "", `${stops.length} stops · ${inspectionCount} inspections`));
    section.append(heading);

    const wrapper = element("div", "table-wrap");
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headingRow = document.createElement("tr");
    ["Sequence", "Address", "Inspection #(s)", "Permit #(s)", "Inspection type(s)"].forEach(
      (label) => headingRow.append(element("th", "", label))
    );
    head.append(headingRow);
    table.append(head);
    const body = document.createElement("tbody");
    stops.forEach((stop) => body.append(renderStop(stop)));
    table.append(body);
    wrapper.append(table);
    section.append(wrapper);
    return section;
  };

  const updateSummary = (inspectors) => {
    const visibleStops = inspectors.flatMap((item) => item.stops);
    document.getElementById("inspector-count").textContent = String(inspectors.length);
    document.getElementById("stop-count").textContent = String(visibleStops.length);
    document.getElementById("inspection-count").textContent = String(
      visibleStops.reduce((sum, stop) => sum + stop.inspectionCount, 0)
    );
    document.getElementById("rollover-count").textContent = String(
      visibleStops.filter((stop) => stop.isRollover).length
    );
  };

  const render = () => {
    const selectedInspector = inspectorFilter.value;
    const query = routeSearch.value.trim().toLocaleLowerCase();
    const visible = data.inspectors
      .filter((inspector) => selectedInspector === "all" || inspector.name === selectedInspector)
      .map((inspector) => ({
        ...inspector,
        stops: inspector.stops.filter((stop) => stopMatches(stop, query))
      }))
      .filter((inspector) => inspector.stops.length);

    routes.replaceChildren();
    visible.forEach((inspector) => routes.append(renderInspector(inspector, inspector.stops)));
    emptyState.hidden = visible.length > 0;
    updateSummary(visible);
  };

  document.getElementById("route-date").textContent = formatDate(data.routeDate);
  document.getElementById("generated-at").textContent = formatDateTime(data.generatedAt);
  document.getElementById("route-description").textContent =
    "Use this recommended order to complete inspections. Inspectors assign times themselves.";
  document.getElementById("rollover-note").textContent =
    `* Presumed rollover stops were incomplete on ${formatDate(data.rolloverSourceDate)}. ` +
    "Ignore an inspection if it has since been completed.";

  data.inspectors.forEach((inspector) => {
    const option = element("option", "", inspector.name);
    option.value = inspector.name;
    inspectorFilter.append(option);
  });

  inspectorFilter.addEventListener("change", render);
  routeSearch.addEventListener("input", render);
  document.getElementById("clear-filters").addEventListener("click", () => {
    inspectorFilter.value = "all";
    routeSearch.value = "";
    render();
  });

  render();
})();
